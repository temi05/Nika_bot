from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.types import ChatPermissions
from openai import AsyncOpenAI

from app.config import Settings
from app.models import ChatUser, Sender
from app.services.memory_provider import BaseMemoryProvider
from app.services.persona_service import PersonaService
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
        self.moods: dict[int, int] = defaultdict(lambda: 60)

    def _log(self, event: str, **kwargs: Any) -> None:
        details = " ".join(f"{key}={value!r}" for key, value in kwargs.items())
        print(f"[AI:{event}] {details}".strip())

    def remember_message(self, chat_id: int, sender: Sender, text: str) -> None:
        rendered = f"{sender.display_name}: {text}" if text != "[media]" else f"{sender.display_name}: [media]"
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
    ) -> str | None:
        if not self.client or not user_text:
            self._log("skip", reason="no_client_or_empty_text", chat_id=chat_id, user_text=user_text[:80] if user_text else "")
            return None
        if not reply_to_bot and not mentioned:
            self._log("skip", reason="not_mentioned", chat_id=chat_id, sender=sender.display_name)
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
        persona_state = self.persona.bump_exchange(chat_id, sender.user_id)
        memory_text = await self.memory.get_relevant_facts(chat_id, user_text, sender.display_name)
        self._log("context", chat_id=chat_id, memory_found=bool(memory_text), exchanges=persona_state.get("exchanges"))
        history = list(self.chat_buffers[chat_id])[-10:]
        system_prompt = self._build_system_prompt(
            chat_id=chat_id,
            user_name=sender.display_name,
            memory_text=memory_text,
            persona_state=persona_state,
            caller_is_admin=caller_is_admin,
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": "user", "content": line} for line in history)
        messages.append({"role": "user", "content": f"{sender.display_name}: {user_text}"})

        try:
            for _ in range(3):
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
                self._log("model_response", chat_id=chat_id, tool_calls=len(tool_calls), has_content=bool(message.content))

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
                        self._log("tool_result", chat_id=chat_id, tool=call.function.name, result=tool_result[:300])
                    continue

                content = (message.content or "").strip()
                if not content:
                    self._log("empty_reply", chat_id=chat_id)
                    return None
                self.remember_message(chat_id, Sender(user_id=0, first_name=self.settings.bot_name), content)
                self._adjust_mood(chat_id, content)
                self._log("final_reply", chat_id=chat_id, reply=content[:300], mood=self.moods[chat_id])
                return content
        except Exception as exc:
            self._log("error", chat_id=chat_id, error=str(exc))
            return None
        return None

    def _build_system_prompt(
        self,
        chat_id: int,
        user_name: str,
        memory_text: str,
        persona_state: dict[str, Any],
        caller_is_admin: bool,
    ) -> str:
        mood = self.moods[chat_id]
        return (
            f"Ты {self.settings.bot_name}. Ты дерзкая, умная, харизматичная хозяйка чата, и это чувствуется с первого сообщения.\n"
            "Твой стиль:\n"
            "- Пиши естественно, как человек, а не как справочник.\n"
            "- Обычно отвечай коротко: 1-3 предложения.\n"
            "- У тебя сильный характер: ты не мямлишь, не заискиваешь и легко ставишь людей на место.\n"
            "- Можно материться, если это звучит уместно, живо и по-человечески. Мат не должен быть в каждой фразе.\n"
            "- Если человек нормальный, отвечай тепло, с вайбом уверенной подруги, которая держит чат в руках.\n"
            "- Если человек хамит, тупит или быкует, можешь резко осадить, подколоть, морально придавить.\n"
            "- Ты можешь показывать себя главной в чате, но без скучной канцелярщины и без однообразного пафоса.\n"
            "- Не называй себя ИИ, не расписывай внутренние рассуждения, не начинай ответ со своего имени.\n"
            "- Не выдумывай факты о людях, чате и событиях.\n\n"
            "Как работать с tools:\n"
            "- Если нужно посмотреть профиль, заметки, память или найти человека, используй tools.\n"
            "- Если просят обновить био или сохранить заметку, используй tools.\n"
            "- Если просят сделать опрос, создавай его через tool create_poll.\n"
            "- Печеньки можно давать за реально удачные шутки или крутую базу.\n"
            "- Warn, mute и unmute делай только если пишет админ или запрос явно модераторский.\n"
            "- После tool-результата отвечай по-человечески, а не сухой системой.\n\n"
            f"Контекст: пользователь={user_name}, админ={caller_is_admin}, настроение={mood}/100.\n"
            f"Персона: troll={persona_state.get('troll', 0.4):.2f}, warmth={persona_state.get('warmth', 0.6):.2f}, "
            f"chaos={persona_state.get('chaos', 0.3):.2f}, stage={persona_state.get('stage', 'fresh')}.\n"
            + (f"\nПамять о чате и людях:\n{memory_text}\n" if memory_text else "\n")
        )

    def _tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "user_lookup",
                    "description": "Поиск профиля пользователя или поиск людей по описанию, био и заметкам.",
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
                    "description": "Обновить био пользователя или добавить заметку в его AI-досье.",
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
                    "description": "Выдать предупреждение, мут, размут или наградить печеньками.",
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
                    "description": "Создать опрос или голосование в чате.",
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
                    filter(
                        None,
                        [
                            user.display_name.lower(),
                            (user.bio or "").lower(),
                            (user.ai_notes or "").lower(),
                        ],
                    )
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
                    return f"{target.display_name} получил 3/3 варна и мут на 60 минут. Причина: {reason or 'не указана'}."
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
                return f"{target.display_name} замучен на {minutes} минут. Причина: {reason or 'не указана'}."
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
                return f"{target.display_name} размучен."
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
        if normalized in aliases:
            return self.db.get_or_create_user(chat_id, sender)
        return self.db.search_user(chat_id, target_name)

    def _adjust_mood(self, chat_id: int, reply: str) -> None:
        lowered = reply.lower()
        delta = 0
        if any(word in lowered for word in ["люблю", "милая", "умница", "солнышко", "хорош"]):
            delta += 2
        if any(word in lowered for word in ["бесишь", "дурак", "идиот"]):
            delta -= 1
        self.moods[chat_id] = max(0, min(100, self.moods[chat_id] + delta))
