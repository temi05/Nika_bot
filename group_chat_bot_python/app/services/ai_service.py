from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from aiogram import Bot
from aiogram.types import ChatPermissions
from openai import AsyncOpenAI

from app.config import Settings
from app.models import ChatUser, Sender
from app.services.memory_provider import BaseMemoryProvider
from app.services.persona_service import PersonaService
from app.services.prompt_builders import build_character_system_prompt
from app.services.supabase_db import SupabaseDB


class AIService:
    def __init__(
        self,
        settings: Settings,
        db: SupabaseDB,
        memory: BaseMemoryProvider,
        persona: PersonaService,
        bot: Bot,
    ) -> None:
        self.settings = settings
        self.db = db
        self.memory = memory
        self.persona = persona
        self.bot = bot
        self.client = (
            AsyncOpenAI(
                api_key=settings.effective_ai_api_key,
                base_url=settings.effective_ai_base_url,
                timeout=settings.ai_timeout_seconds,
            )
            if settings.effective_ai_api_key
            else None
        )
        self.chat_buffers: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=25))
        self.recent_bot_replies: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=8))
        self.moods: dict[int, int] = defaultdict(lambda: 60)
        self.last_group_reply_at: dict[int, datetime] = {}

    def _log(self, event: str, **kwargs: Any) -> None:
        details = " ".join(f"{key}={value!r}" for key, value in kwargs.items())
        print(f"[AI:{event}] {details}".strip())

    def remember_message(self, chat_id: int, sender: Sender, text: str) -> None:
        rendered = f"{sender.display_name}: {text}"
        self.chat_buffers[chat_id].append(rendered)
        self._log("remember", chat_id=chat_id, sender=sender.display_name, text=rendered[:160])

    async def flush_passive_memory(self, chat_id: int) -> None:
        if len(self.chat_buffers[chat_id]) < 25:
            return

        transcript = "\n".join(self.chat_buffers[chat_id])
        participants = list(dict.fromkeys(line.split(":", 1)[0] for line in self.chat_buffers[chat_id]))
        self._log("flush_memory_start", chat_id=chat_id, participants=participants, lines=len(self.chat_buffers[chat_id]))
        await self.memory.save_transcript(chat_id, transcript, participants)
        self.chat_buffers[chat_id].clear()
        self._log("flush_memory_done", chat_id=chat_id)

    async def generate_reply(
        self,
        chat_id: int,
        sender: Sender,
        user_text: str,
        reply_to_bot: bool,
        mentioned: bool,
        caller_is_admin: bool = False,
        is_private_chat: bool = False,
    ) -> str | None:
        if not user_text:
            self._log("skip", reason="empty_text", chat_id=chat_id)
            return None

        direct_reply = await self._maybe_handle_direct_action(chat_id, sender, user_text)
        if direct_reply:
            self.remember_message(chat_id, Sender(user_id=0, first_name=self.settings.bot_name), direct_reply)
            self._remember_bot_reply(chat_id, direct_reply)
            self._adjust_mood(chat_id, direct_reply)
            self._mark_group_reply(chat_id, is_private_chat=is_private_chat)
            self._log("direct_action_reply", chat_id=chat_id, reply=direct_reply[:200])
            return direct_reply

        if not self.client:
            self._log("skip", reason="no_client", chat_id=chat_id)
            return "Я сейчас без доступа к модели. Попробуй чуть позже."
        if not self.settings.effective_ai_api_key:
            self._log("skip", reason="no_api_key", chat_id=chat_id)
            return "Я сейчас без доступа к модели. Попробуй чуть позже."

        if not self.settings.ai_model:
            self._log("skip", reason="no_ai_model", chat_id=chat_id)
            return "Модель ответа не настроена."

        if not self.settings.effective_ai_base_url:
            self._log("skip", reason="no_base_url", chat_id=chat_id)
            return None

        addressed = mentioned or reply_to_bot
        is_media_message = self._is_media_marker(user_text)
        if not is_private_chat:
            if not addressed and is_media_message:
                self._log("skip", reason="media_without_mention", chat_id=chat_id)
                return None
            if not addressed and len(user_text.strip()) < self.settings.ai_min_message_len:
                self._log("skip", reason="too_short_in_group", chat_id=chat_id)
                return None
            if not addressed and self._group_reply_cooldown_active(chat_id):
                self._log("skip", reason="group_cooldown", chat_id=chat_id)
                return None

        self._log(
            "start",
            chat_id=chat_id,
            sender=sender.display_name,
            reply_to_bot=reply_to_bot,
            mentioned=mentioned,
            caller_is_admin=caller_is_admin,
            user_text=user_text[:200],
        )

        self.persona.observe_user_message(
            chat_id,
            sender.user_id,
            user_text,
            reply_to_bot=reply_to_bot,
            mentioned=mentioned,
        )
        self._adjust_mood_from_user_message(chat_id, user_text)
        persona_state = self.persona.bump_exchange(chat_id, sender.user_id)

        memory_text = await self.memory.get_relevant_facts(chat_id, user_text, sender.display_name)
        self._log("context", chat_id=chat_id, memory_found=bool(memory_text), exchanges=persona_state.get("exchanges"))

        system_prompt = build_character_system_prompt(
            bot_name=self.settings.bot_name,
            user_name=sender.display_name,
            caller_is_admin=caller_is_admin,
            mood=self.moods[chat_id],
            persona_state=persona_state,
            memory_context=memory_text,
            personality_mode=self.settings.bot_personality_mode,
            compact_prompt=self.settings.ai_compact_prompt,
        )

        history = list(self.chat_buffers[chat_id])[-self.settings.ai_history_lines :]
        current_line = f"{sender.display_name}: {user_text}"
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": "user", "content": line} for line in history)
        if not history or history[-1] != current_line:
            messages.append({"role": "user", "content": current_line})

        try:
            for round_index in range(3):
                response = await self.client.chat.completions.create(
                    model=self.settings.ai_model,
                    messages=messages,
                    tools=self._tool_definitions(),
                    tool_choice="auto",
                    temperature=self.settings.ai_temperature,
                    max_tokens=self.settings.ai_max_tokens,
                )
                message = response.choices[0].message
                tool_calls = list(message.tool_calls or [])
                self._log("model_response", chat_id=chat_id, round=round_index + 1, tool_calls=len(tool_calls))

                if tool_calls:
                    assistant_message: dict[str, Any] = {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [],
                    }
                    for call in tool_calls:
                        assistant_message["tool_calls"].append(
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.function.name,
                                    "arguments": call.function.arguments,
                                },
                            }
                        )
                    messages.append(assistant_message)

                    for call in tool_calls:
                        self._log("tool_call", chat_id=chat_id, tool=call.function.name, raw_arguments=call.function.arguments)
                        tool_result = await self._run_tool_call(
                            chat_id=chat_id,
                            sender=sender,
                            caller_is_admin=caller_is_admin,
                            tool_name=call.function.name,
                            raw_arguments=call.function.arguments,
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": tool_result,
                            }
                        )
                        self._log("tool_result", chat_id=chat_id, tool=call.function.name, result=tool_result[:240])
                    continue

                raw_text = self._coerce_model_content(message.content)
                content = self._finalize_reply(raw_text, user_text=user_text)
                if not content:
                    rescued = await self._retry_empty_reply(messages, sender.display_name, user_text)
                    if rescued:
                        rescued = await self._ensure_non_repetitive_reply(
                            chat_id=chat_id,
                            messages=messages,
                            sender_name=sender.display_name,
                            user_text=user_text,
                            content=rescued,
                        )
                        self.remember_message(chat_id, Sender(user_id=0, first_name=self.settings.bot_name), rescued)
                        self._remember_bot_reply(chat_id, rescued)
                        self._adjust_mood(chat_id, rescued)
                        self._mark_group_reply(chat_id, is_private_chat=is_private_chat)
                        self._log("empty_reply_recovered", chat_id=chat_id, reply=rescued[:180])
                        return rescued
                    self._log("empty_reply", chat_id=chat_id)
                    return None

                content = await self._ensure_non_repetitive_reply(
                    chat_id=chat_id,
                    messages=messages,
                    sender_name=sender.display_name,
                    user_text=user_text,
                    content=content,
                )
                self.remember_message(chat_id, Sender(user_id=0, first_name=self.settings.bot_name), content)
                self._remember_bot_reply(chat_id, content)
                self._adjust_mood(chat_id, content)
                self._mark_group_reply(chat_id, is_private_chat=is_private_chat)
                self._log("final_reply", chat_id=chat_id, reply=content[:240], mood=self.moods[chat_id])
                return content
        except Exception as exc:
            self._log("error", chat_id=chat_id, error=str(exc))
            return "Упс, зависла на ответе. Напиши ещё раз."
        self._log("fallback_after_rounds", chat_id=chat_id)
        return "Сделала всё, что смогла. Уточни, что именно нужно."

    async def _maybe_handle_direct_action(self, chat_id: int, sender: Sender, user_text: str) -> str | None:
        memory_text = self._extract_memory_request(user_text)
        if memory_text:
            user = self.db.get_or_create_user(chat_id, sender)
            self.db.append_ai_note(user, memory_text)
            self._log("direct_memory_saved", chat_id=chat_id, sender=sender.display_name, text=memory_text[:160])
            return f"Запомнила: {memory_text}"

        poll_request = self._extract_poll_request(user_text)
        if not poll_request:
            return None

        result = await self._tool_create_poll(chat_id, poll_request)
        if "Опрос создан." in result:
            return "Сделала опрос. Голосуйте."
        return f"Не получилось сделать опрос: {result}"

    def _extract_memory_request(self, user_text: str) -> str | None:
        text = user_text.strip()
        if not text:
            return None

        bot_names = [self.settings.bot_name]
        if self.settings.bot_username:
            bot_names.append(f"@{self.settings.bot_username}")
        for name in filter(None, bot_names):
            text = re.sub(rf"^\s*{re.escape(name)}\s*[,;:\-–—]?\s*", "", text, flags=re.IGNORECASE)

        match = re.search(
            r"(?is)\b(?:запомни|запиши|сохрани|remember)\b\s*(?:,?\s*(?:что|это|себе|that))?\s*[:\-–—]?\s*(.+)",
            text,
        )
        if not match:
            return None

        memory_text = re.sub(r"\s+", " ", match.group(1)).strip(" .")
        if len(memory_text) < 4:
            return None
        return memory_text[:300]

    def _extract_poll_request(self, user_text: str) -> dict[str, Any] | None:
        lowered = user_text.lower()
        if not any(keyword in lowered for keyword in ["опрос", "голосован", "poll"]):
            return None

        body = user_text.split(":", 1)[1].strip() if ":" in user_text else user_text
        body = re.sub(r"(?i)\b(сделай|создай|запусти|устрой|делаем)\b", "", body).strip()
        body = re.sub(r"(?i)\b(опрос|голосование|poll)\b", "", body).strip(" .,-")

        options: list[str] = []
        if "," in body:
            options = [part.strip() for part in body.split(",") if part.strip()]
        elif ";" in body:
            options = [part.strip() for part in body.split(";") if part.strip()]
        elif re.search(r"(?i)\s+или\s+", body):
            options = [part.strip() for part in re.split(r"(?i)\s+или\s+", body) if part.strip()]

        if len(options) < 2:
            return None

        question = "Что выбираем?"
        if "кто" in lowered:
            question = "Кто победит?"
        elif "лучше" in lowered:
            question = "Что лучше?"

        return {
            "question": question,
            "options": options[:10],
            "is_anonymous": True,
            "allows_multiple_answers": False,
        }

    def _tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "user_lookup",
                    "description": "Find user profile or search users by notes and bio in this chat.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["profile", "search"]},
                            "query": {"type": "string"},
                        },
                        "required": ["action", "query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "manage_user_profile",
                    "description": "Update user bio or append an AI note.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "target_name": {"type": "string"},
                            "action": {"type": "string", "enum": ["update_bio", "add_note"]},
                            "content": {"type": "string"},
                        },
                        "required": ["target_name", "action", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "moderate_user",
                    "description": "Warn/mute/unmute or reward a user in this chat.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "target_name": {"type": "string"},
                            "action": {"type": "string", "enum": ["warn", "mute", "unmute", "reward"]},
                            "value": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["target_name", "action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_poll",
                    "description": "Create a Telegram poll in current chat.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "options": {"type": "array", "items": {"type": "string"}},
                            "is_anonymous": {"type": "boolean"},
                            "allows_multiple_answers": {"type": "boolean"},
                        },
                        "required": ["question", "options"],
                    },
                },
            },
        ]

    async def _run_tool_call(
        self,
        chat_id: int,
        sender: Sender,
        caller_is_admin: bool,
        tool_name: str,
        raw_arguments: str,
    ) -> str:
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            self._log("tool_args_error", tool=tool_name, raw_arguments=raw_arguments)
            return "Не смогла разобрать аргументы инструмента."

        if tool_name == "user_lookup":
            return self._tool_user_lookup(chat_id, sender, args)
        if tool_name == "manage_user_profile":
            return self._tool_manage_user_profile(chat_id, sender, args)
        if tool_name == "moderate_user":
            return await self._tool_moderate_user(chat_id, caller_is_admin, args)
        if tool_name == "create_poll":
            return await self._tool_create_poll(chat_id, args)
        return "Неизвестный инструмент."

    def _tool_user_lookup(self, chat_id: int, sender: Sender, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        query = str(args.get("query") or "").strip()
        if not query:
            return "Пустой запрос."

        if action == "search":
            users = self.db.get_all_users(chat_id)
            normalized = query.lower()
            matches = []
            for user in users:
                haystack = " | ".join(
                    filter(None, [user.display_name.lower(), (user.bio or "").lower(), (user.ai_notes or "").lower()])
                )
                if normalized in haystack:
                    matches.append(user.display_name)
            if not matches:
                self._log("user_lookup_search_empty", chat_id=chat_id, query=query)
                return "Никого не нашла."
            self._log("user_lookup_search", chat_id=chat_id, query=query, matches=matches[:8])
            return "Нашла:\n" + "\n".join(f"- {name}" for name in matches[:8])

        target = self._resolve_target_user(chat_id, sender, query)
        if not target:
            self._log("user_lookup_profile_missing", chat_id=chat_id, query=query)
            return f"Не нашла пользователя: {query}"

        facts = self.db.get_all_user_facts(chat_id, target.display_name, limit=6)
        lines = [
            f"Профиль: {target.display_name}",
            f"Уровень: {target.level}",
            f"XP: {target.xp}",
            f"Печеньки: {target.reputation}",
            f"Варны: {target.warns}/{self.settings.warn_limit}",
            f"Био: {target.bio or 'нет'}",
            f"Заметки: {target.ai_notes or 'нет'}",
        ]
        if facts:
            lines.append("Факты:")
            lines.extend(f"- {fact}" for fact in facts[:4])
        self._log("user_lookup_profile", chat_id=chat_id, query=query, target=target.display_name)
        return "\n".join(lines)

    def _tool_manage_user_profile(self, chat_id: int, sender: Sender, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        target_name = str(args.get("target_name") or "").strip()
        content = str(args.get("content") or "").strip()
        if not content:
            return "Пустой контент."

        target = self._resolve_target_user(chat_id, sender, target_name)
        if not target:
            self._log("manage_profile_missing", chat_id=chat_id, target_name=target_name, action=action)
            return "Не нашла пользователя для обновления профиля."

        if action == "update_bio":
            self.db.set_bio(target, content)
            self._log("manage_profile_bio", chat_id=chat_id, target=target.display_name, content=content[:150])
            return f"Био для {target.display_name} обновлено."
        if action == "add_note":
            self.db.append_ai_note(target, content)
            self._log("manage_profile_note", chat_id=chat_id, target=target.display_name, content=content[:150])
            return f"Заметка о {target.display_name} сохранена."
        return "Неизвестное действие профиля."

    async def _tool_moderate_user(self, chat_id: int, caller_is_admin: bool, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        target_name = str(args.get("target_name") or "").strip()
        reason = str(args.get("reason") or "").strip()
        value = int(args.get("value") or 0)

        if not target_name:
            return "Не указана цель."

        target = self.db.search_user(chat_id, target_name)
        if not target:
            self._log("moderation_target_missing", chat_id=chat_id, target_name=target_name, action=action)
            return f"Не нашла пользователя: {target_name}"

        if action == "reward":
            amount = min(max(value or 1, 1), 3)
            self.db.update_user(target.id, {"reputation": target.reputation + amount})
            self._log("moderation_reward", chat_id=chat_id, target=target.display_name, amount=amount)
            return f"{target.display_name} получил {amount} печенек."

        if not caller_is_admin:
            self._log("moderation_denied", chat_id=chat_id, target=target.display_name, action=action)
            return "Наказания через AI доступны только админам."

        if action == "warn":
            updated = self.db.apply_warn(target)
            warns = updated.warns if updated else target.warns + 1
            if warns >= self.settings.warn_limit:
                try:
                    await self.bot.restrict_chat_member(
                        chat_id,
                        target.user_id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=datetime.now() + timedelta(minutes=60),
                    )
                    self.db.clear_warns(target)
                    self._log("moderation_warn_mute", chat_id=chat_id, target=target.display_name, reason=reason)
                    return (
                        f"{target.display_name} получил {self.settings.warn_limit}/{self.settings.warn_limit} варнов "
                        f"и мут на 60 минут. Причина: {reason or 'не указана'}."
                    )
                except Exception as exc:
                    self._log("moderation_warn_mute_error", chat_id=chat_id, target=target.display_name, error=str(exc))
                    return f"{target.display_name} получил варн {warns}/{self.settings.warn_limit}, но мут не сработал: {exc}"
            self._log("moderation_warn", chat_id=chat_id, target=target.display_name, warns=warns, reason=reason)
            return f"{target.display_name} получил варн {warns}/{self.settings.warn_limit}. Причина: {reason or 'не указана'}."

        if action == "mute":
            minutes = min(max(value or 15, 1), 1440)
            try:
                await self.bot.restrict_chat_member(
                    chat_id,
                    target.user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=datetime.now() + timedelta(minutes=minutes),
                )
                self._log("moderation_mute", chat_id=chat_id, target=target.display_name, minutes=minutes, reason=reason)
                return f"{target.display_name} замьючен на {minutes} минут. Причина: {reason or 'не указана'}."
            except Exception as exc:
                self._log("moderation_mute_error", chat_id=chat_id, target=target.display_name, error=str(exc))
                return f"Не смогла выдать мут: {exc}"

        if action == "unmute":
            try:
                await self.bot.restrict_chat_member(
                    chat_id,
                    target.user_id,
                    permissions=ChatPermissions(
                        can_send_messages=True,
                        can_send_audios=True,
                        can_send_documents=True,
                        can_send_photos=True,
                        can_send_videos=True,
                        can_send_video_notes=True,
                        can_send_voice_notes=True,
                        can_send_polls=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True,
                        can_invite_users=True,
                    ),
                )
                self._log("moderation_unmute", chat_id=chat_id, target=target.display_name)
                return f"{target.display_name} размьючен."
            except Exception as exc:
                self._log("moderation_unmute_error", chat_id=chat_id, target=target.display_name, error=str(exc))
                return f"Не смогла снять мут: {exc}"

        return "Неизвестное действие модерации."

    async def _tool_create_poll(self, chat_id: int, args: dict[str, Any]) -> str:
        question = str(args.get("question") or "").strip()[:300]
        options = args.get("options") or []
        is_anonymous = bool(args.get("is_anonymous", True))
        allows_multiple_answers = bool(args.get("allows_multiple_answers", False))

        if not question:
            return "Не смогла создать опрос: пустой вопрос."
        if not isinstance(options, list):
            return "Не смогла создать опрос: варианты должны быть списком."

        safe_options = [str(option).strip()[:100] for option in options if str(option).strip()]
        if len(safe_options) < 2:
            return "Не смогла создать опрос: нужно минимум 2 варианта."

        try:
            await self.bot.send_poll(
                chat_id=chat_id,
                question=question,
                options=safe_options[:10],
                is_anonymous=is_anonymous,
                allows_multiple_answers=allows_multiple_answers,
            )
            self._log(
                "create_poll",
                chat_id=chat_id,
                question=question,
                options=safe_options[:10],
                is_anonymous=is_anonymous,
                allows_multiple_answers=allows_multiple_answers,
            )
            return "Опрос создан."
        except Exception as exc:
            self._log("create_poll_error", chat_id=chat_id, error=str(exc), question=question)
            return f"Не смогла создать опрос: {exc}"

    def _resolve_target_user(self, chat_id: int, sender: Sender, target_name: str) -> ChatUser | None:
        normalized = (target_name or "").strip().lower()
        aliases = {"я", "me", "мой", "мне", sender.display_name.lower(), sender.first_name.lower()}
        if sender.username:
            aliases.add(f"@{sender.username.lower()}")
        if normalized in aliases:
            return self.db.get_or_create_user(chat_id, sender)
        return self.db.search_user(chat_id, target_name)

    def _group_reply_cooldown_active(self, chat_id: int) -> bool:
        seconds = max(0, self.settings.ai_group_cooldown_seconds)
        if seconds == 0:
            return False
        last = self.last_group_reply_at.get(chat_id)
        if not last:
            return False
        return (datetime.utcnow() - last).total_seconds() < seconds

    def _mark_group_reply(self, chat_id: int, *, is_private_chat: bool) -> None:
        if is_private_chat:
            return
        self.last_group_reply_at[chat_id] = datetime.utcnow()

    def _adjust_mood(self, chat_id: int, reply: str) -> None:
        lowered = reply.lower()
        delta = 0
        if any(word in lowered for word in ["люблю", "милая", "умница", "солнышко", "хорош", "красава"]):
            delta += 2
        if any(word in lowered for word in ["бесишь", "дурак", "идиот", "нахуй", "заебал"]):
            delta -= 2
        self.moods[chat_id] = max(0, min(100, self.moods[chat_id] + delta))

    def _adjust_mood_from_user_message(self, chat_id: int, user_text: str) -> None:
        lowered = user_text.lower()
        delta = 0
        if any(word in lowered for word in ["спасибо", "обожаю", "люблю", "умница", "красава"]):
            delta += 4
        if any(word in lowered for word in ["хуево", "хуёво", "туп", "глуп", "идиот", "нахуй", "пизд", "еба", "пошла"]):
            delta -= 6
        self.moods[chat_id] = max(0, min(100, self.moods[chat_id] + delta))

    def _finalize_reply(self, content: str, *, user_text: str) -> str:
        cleaned = content.strip()
        if not cleaned:
            return ""

        bot_name = re.escape(self.settings.bot_name)
        cleaned = re.sub(rf"^(?:{bot_name}\s*:\s*)+", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"^\s*нейроника\s*:\s*", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)

        if self._is_hostile_user_text(user_text):
            cleaned = re.sub(
                r"(?:\s*(?:А|Ну а|И)\s+у\s+тебя\s+как.*|\s*Как\s+у\s+тебя.*|\s*Чем\s+занят.*|\s*Что\s+конкретно\s+интересует\??)\s*$",
                "",
                cleaned,
                flags=re.IGNORECASE,
            ).strip(" -,.!?")

        cleaned = self._enforce_personality_mode(cleaned, user_text=user_text)
        return cleaned

    def _enforce_personality_mode(self, content: str, *, user_text: str) -> str:
        # Keep responses model-driven (no deterministic template substitution).
        # We only clean obvious filler phrasing.
        return self._strip_soft_phrases(content)

    def _strip_soft_phrases(self, content: str) -> str:
        cleaned = content
        soft_phrases = [
            "радость моя",
            "заяц",
            "дерзкий заяц",
            "магия",
            "сюрпризам",
            "сюрпризы",
            "малой",
            "золотце",
            "милый",
            "милая",
        ]
        for phrase in soft_phrases:
            cleaned = re.sub(rf"\b{re.escape(phrase)}\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([,!.?])", r"\1", cleaned)
        return cleaned.strip(" ,")

    def _looks_too_soft(self, content: str) -> bool:
        lowered = content.lower()
        soft_markers = [
            "радость моя",
            "заяц",
            "магия",
            "сюрприз",
            "не оставлять тебя равнодушным",
            "приготовься",
            "как там дела",
        ]
        return any(marker in lowered for marker in soft_markers)

    def _is_hostile_user_text(self, user_text: str) -> bool:
        lowered = user_text.lower()
        hostile_tokens = [
            "хуево",
            "хуёво",
            "туп",
            "глуп",
            "идиот",
            "дура",
            "глупая голова",
            "нахуй",
            "пизд",
            "еба",
            "пошла",
            "отвечаешь как-то",
        ]
        return any(token in lowered for token in hostile_tokens)

    def _is_media_marker(self, user_text: str) -> bool:
        return user_text.strip().lower().startswith("[media:")

    def _remember_bot_reply(self, chat_id: int, reply: str) -> None:
        normalized = self._normalize_reply_key(reply)
        if not normalized:
            return
        self.recent_bot_replies[chat_id].append(normalized)

    def _normalize_reply_key(self, text: str) -> str:
        key = text.strip().lower()
        key = re.sub(r"\s+", " ", key)
        key = re.sub(r"[^\w\s]", "", key, flags=re.UNICODE)
        key = re.sub(r"\s{2,}", " ", key).strip()
        return key

    def _is_repetitive_reply(self, chat_id: int, reply: str) -> bool:
        key = self._normalize_reply_key(reply)
        if len(key) < 8:
            return False
        for previous in self.recent_bot_replies[chat_id]:
            if key == previous:
                return True
            if len(key) > 20 and len(previous) > 20:
                if SequenceMatcher(None, key, previous).ratio() >= 0.94:
                    return True
        return False

    async def _ensure_non_repetitive_reply(
        self,
        chat_id: int,
        messages: list[dict[str, Any]],
        sender_name: str,
        user_text: str,
        content: str,
    ) -> str:
        if not self._is_repetitive_reply(chat_id, content):
            return content

        self._log("repetition_detected", chat_id=chat_id, content=content[:160])
        retry = await self._retry_repetitive_reply(
            messages=messages,
            sender_name=sender_name,
            user_text=user_text,
            previous_reply=content,
        )
        if retry and not self._is_repetitive_reply(chat_id, retry):
            self._log("repetition_fixed", chat_id=chat_id, retry=retry[:160])
            return retry

        self._log("repetition_keep_original", chat_id=chat_id)
        return content

    async def _retry_repetitive_reply(
        self,
        *,
        messages: list[dict[str, Any]],
        sender_name: str,
        user_text: str,
        previous_reply: str,
    ) -> str:
        retry_messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    f"{sender_name}: Дай новый ответ на сообщение '{user_text}', "
                    f"но не повторяй прошлую формулировку: '{previous_reply}'. "
                    "Ответ должен быть коротким и живым."
                ),
            },
        ]

        try:
            response = await self.client.chat.completions.create(
                model=self.settings.ai_model,
                messages=retry_messages,
                temperature=max(self.settings.ai_temperature, 0.9),
                max_tokens=min(self.settings.ai_max_tokens, 140),
            )
            message = response.choices[0].message
            raw_text = self._coerce_model_content(message.content)
            return self._finalize_reply(raw_text, user_text=user_text)
        except Exception as exc:
            self._log("repetition_retry_error", error=str(exc))
            return ""

    def _coerce_model_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if isinstance(item, str):
                    chunks.append(item)
                    continue
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        chunks.append(text.strip())
            return " ".join(chunks).strip()
        return ""

    async def _retry_empty_reply(self, messages: list[dict[str, Any]], user_name: str, user_text: str) -> str:
        retry_messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    f"{user_name}: Ответь на предыдущее сообщение одной короткой живой репликой "
                    f"без молчания и без пустых фраз. Исходная реплика: {user_text}"
                ),
            },
        ]
        try:
            response = await self.client.chat.completions.create(
                model=self.settings.ai_model,
                messages=retry_messages,
                temperature=self.settings.ai_temperature,
                max_tokens=min(self.settings.ai_max_tokens, 140),
            )
            message = response.choices[0].message
            raw_text = self._coerce_model_content(message.content)
            return self._finalize_reply(raw_text, user_text=user_text)
        except Exception as exc:
            self._log("empty_reply_retry_error", error=str(exc))
            return ""
