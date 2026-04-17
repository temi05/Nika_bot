from __future__ import annotations

import json
import re
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
        self.moods: dict[int, int] = defaultdict(lambda: 60)
        self.last_group_reply_at: dict[int, datetime] = {}

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
        is_private_chat: bool = False,
    ) -> str | None:
        if not self.client or not user_text:
            self._log("skip", reason="no_client_or_empty_text", chat_id=chat_id)
            return None

        if not is_private_chat:
            if not mentioned and user_text.strip() == "[media]":
                self._log("skip", reason="media_without_mention", chat_id=chat_id)
                return None
            if not mentioned and len(user_text.strip()) < self.settings.ai_min_message_len:
                self._log("skip", reason="too_short_in_group", chat_id=chat_id)
                return None
            if self._group_reply_cooldown_active(chat_id):
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

        direct_reply = await self._maybe_handle_direct_action(chat_id, sender, user_text)
        if direct_reply:
            self.remember_message(chat_id, Sender(user_id=0, first_name=self.settings.bot_name), direct_reply)
            self._adjust_mood(chat_id, direct_reply)
            self._mark_group_reply(chat_id, is_private_chat=is_private_chat)
            self._log("direct_action_reply", chat_id=chat_id, reply=direct_reply[:200])
            return direct_reply

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
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": "user", "content": line} for line in history)
        messages.append({"role": "user", "content": f"{sender.display_name}: {user_text}"})

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

                content = self._finalize_reply((message.content or "").strip(), user_text=user_text)
                if not content:
                    self._log("empty_reply", chat_id=chat_id)
                    return None

                self.remember_message(chat_id, Sender(user_id=0, first_name=self.settings.bot_name), content)
                self._adjust_mood(chat_id, content)
                self._mark_group_reply(chat_id, is_private_chat=is_private_chat)
                self._log("final_reply", chat_id=chat_id, reply=content[:240], mood=self.moods[chat_id])
                return content
        except Exception as exc:
            self._log("error", chat_id=chat_id, error=str(exc))
            return None
        return None

    async def _maybe_handle_direct_action(self, chat_id: int, sender: Sender, user_text: str) -> str | None:
        poll_request = self._extract_poll_request(user_text)
        if not poll_request:
            return None

        result = await self._tool_create_poll(chat_id, poll_request)
        if "РћРїСЂРѕСЃ СЃРѕР·РґР°РЅ" in result:
            return "С‰Р°СЃ СѓСЃС‚СЂРѕРёР»Р° РіРѕР»РѕСЃРѕРІР°РЅРёРµ, РїРѕСЃРјРѕС‚СЂРёРј РєС‚Рѕ С‚СѓС‚ РІРѕРѕР±С‰Рµ РІРјРµРЅСЏРµРјС‹Р№"
        return f"С…РѕС‚РµР»Р° РїРѕРґРЅСЏС‚СЊ РѕРїСЂРѕСЃ, РЅРѕ С‡С‚Рѕ-С‚Рѕ РїРѕС€Р»Рѕ РїРѕ РїРёР·РґРµ: {result}"

    def _extract_poll_request(self, user_text: str) -> dict[str, Any] | None:
        lowered = user_text.lower()
        if not any(keyword in lowered for keyword in ["РѕРїСЂРѕСЃ", "РіРѕР»РѕСЃРѕРІР°РЅ", "poll"]):
            return None

        body = user_text.split(":", 1)[1].strip() if ":" in user_text else user_text
        body = re.sub(r"(?i)\b(СЃРґРµР»Р°Р№|СЃРѕР·РґР°Р№|Р·Р°РїСѓСЃС‚Рё|СѓСЃС‚СЂРѕР№)\b", "", body).strip()
        body = re.sub(r"(?i)\b(РѕРїСЂРѕСЃ|РіРѕР»РѕСЃРѕРІР°РЅРёРµ|poll)\b", "", body).strip(" .,-")

        options: list[str] = []
        if "," in body:
            options = [part.strip() for part in body.split(",") if part.strip()]
        elif ";" in body:
            options = [part.strip() for part in body.split(";") if part.strip()]
        elif " РёР»Рё " in lowered:
            options = [part.strip() for part in re.split(r"(?i)\s+РёР»Рё\s+", body) if part.strip()]

        if len(options) < 2:
            return None

        question = "Р§С‚Рѕ РІС‹Р±РёСЂР°РµРј?"
        if "РєС‚Рѕ" in lowered:
            question = "РќСѓ Рё РєС‚Рѕ С‚СѓС‚ РїРѕР±РµРґРёС‚?"
        elif "Р»СѓС‡С€Рµ" in lowered:
            question = "Р§С‚Рѕ Р»СѓС‡С€Рµ?"
        elif "РІС‹Р±РµСЂРё" in lowered or "РІС‹Р±РёСЂР°РµРј" in lowered:
            question = "Р§С‚Рѕ РІС‹Р±РёСЂР°РµРј?"

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
                    "description": "РќР°Р№С‚Рё РїСЂРѕС„РёР»СЊ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ РёР»Рё Р»СЋРґРµР№ РїРѕ РѕРїРёСЃР°РЅРёСЋ, Р±РёРѕ Рё Р·Р°РјРµС‚РєР°Рј.",
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
                    "description": "РћР±РЅРѕРІРёС‚СЊ Р±РёРѕ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ РёР»Рё РґРѕР±Р°РІРёС‚СЊ Р·Р°РјРµС‚РєСѓ РІ РµРіРѕ AI-РґРѕСЃСЊРµ.",
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
                    "description": "Р’С‹РґР°С‚СЊ РїСЂРµРґСѓРїСЂРµР¶РґРµРЅРёРµ, РјСѓС‚, СЂР°Р·РјСѓС‚ РёР»Рё РЅР°РіСЂР°РґРёС‚СЊ РїРµС‡РµРЅСЊРєР°РјРё.",
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
                    "description": "РЎРѕР·РґР°С‚СЊ РѕРїСЂРѕСЃ РёР»Рё РіРѕР»РѕСЃРѕРІР°РЅРёРµ РІ С‡Р°С‚Рµ.",
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
            return "РќРµ СЃРјРѕРіР»Р° СЂР°Р·РѕР±СЂР°С‚СЊ Р°СЂРіСѓРјРµРЅС‚С‹ РёРЅСЃС‚СЂСѓРјРµРЅС‚Р°."

        if tool_name == "user_lookup":
            return self._tool_user_lookup(chat_id, sender, args)
        if tool_name == "manage_user_profile":
            return self._tool_manage_user_profile(chat_id, sender, args)
        if tool_name == "moderate_user":
            return await self._tool_moderate_user(chat_id, caller_is_admin, args)
        if tool_name == "create_poll":
            return await self._tool_create_poll(chat_id, args)
        return "РќРµРёР·РІРµСЃС‚РЅС‹Р№ РёРЅСЃС‚СЂСѓРјРµРЅС‚."

    def _tool_user_lookup(self, chat_id: int, sender: Sender, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        query = str(args.get("query") or "").strip()
        if not query:
            return "РџСѓСЃС‚РѕР№ Р·Р°РїСЂРѕСЃ."

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
                return "РќРёРєРѕРіРѕ РЅРµ РЅР°С€Р»Р°."
            self._log("user_lookup_search", chat_id=chat_id, query=query, matches=matches[:8])
            return "РќР°С€Р»Р°:\n" + "\n".join(f"- {name}" for name in matches[:8])

        target = self._resolve_target_user(chat_id, sender, query)
        if not target:
            self._log("user_lookup_profile_missing", chat_id=chat_id, query=query)
            return f"РќРµ РЅР°С€Р»Р° РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ: {query}"

        facts = self.db.get_all_user_facts(chat_id, target.display_name, limit=6)
        lines = [
            f"РџСЂРѕС„РёР»СЊ: {target.display_name}",
            f"РЈСЂРѕРІРµРЅСЊ: {target.level}",
            f"XP: {target.xp}",
            f"РџРµС‡РµРЅСЊРєРё: {target.reputation}",
            f"Р’Р°СЂРЅС‹: {target.warns}/{self.settings.warn_limit}",
            f"Р‘РёРѕ: {target.bio or 'РЅРµС‚'}",
            f"Р—Р°РјРµС‚РєРё: {target.ai_notes or 'РЅРµС‚'}",
        ]
        if facts:
            lines.append("Р¤Р°РєС‚С‹:")
            lines.extend(f"- {fact}" for fact in facts[:4])
        self._log("user_lookup_profile", chat_id=chat_id, query=query, target=target.display_name)
        return "\n".join(lines)

    def _tool_manage_user_profile(self, chat_id: int, sender: Sender, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        target_name = str(args.get("target_name") or "").strip()
        content = str(args.get("content") or "").strip()
        if not content:
            return "РџСѓСЃС‚РѕР№ РєРѕРЅС‚РµРЅС‚."

        target = self._resolve_target_user(chat_id, sender, target_name)
        if not target:
            self._log("manage_profile_missing", chat_id=chat_id, target_name=target_name, action=action)
            return "РќРµ РЅР°С€Р»Р° РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ РґР»СЏ РѕР±РЅРѕРІР»РµРЅРёСЏ РїСЂРѕС„РёР»СЏ."

        if action == "update_bio":
            self.db.set_bio(target, content)
            self._log("manage_profile_bio", chat_id=chat_id, target=target.display_name, content=content[:150])
            return f"Р‘РёРѕ РґР»СЏ {target.display_name} РѕР±РЅРѕРІР»РµРЅРѕ."
        if action == "add_note":
            self.db.append_ai_note(target, content)
            self._log("manage_profile_note", chat_id=chat_id, target=target.display_name, content=content[:150])
            return f"Р—Р°РјРµС‚РєР° Рѕ {target.display_name} СЃРѕС…СЂР°РЅРµРЅР°."
        return "РќРµРёР·РІРµСЃС‚РЅРѕРµ РґРµР№СЃС‚РІРёРµ РїСЂРѕС„РёР»СЏ."

    async def _tool_moderate_user(self, chat_id: int, caller_is_admin: bool, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        target_name = str(args.get("target_name") or "").strip()
        reason = str(args.get("reason") or "").strip()
        value = int(args.get("value") or 0)

        if not target_name:
            return "РќРµ СѓРєР°Р·Р°РЅР° С†РµР»СЊ."

        target = self.db.search_user(chat_id, target_name)
        if not target:
            self._log("moderation_target_missing", chat_id=chat_id, target_name=target_name, action=action)
            return f"РќРµ РЅР°С€Р»Р° РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ: {target_name}"

        if action == "reward":
            amount = min(max(value or 1, 1), 3)
            self.db.update_user(target.id, {"reputation": target.reputation + amount})
            self._log("moderation_reward", chat_id=chat_id, target=target.display_name, amount=amount)
            return f"{target.display_name} РїРѕР»СѓС‡РёР» {amount} РїРµС‡РµРЅРµРє."

        if not caller_is_admin:
            self._log("moderation_denied", chat_id=chat_id, target=target.display_name, action=action)
            return "РќР°РєР°Р·Р°РЅРёСЏ С‡РµСЂРµР· AI РґРѕСЃС‚СѓРїРЅС‹ С‚РѕР»СЊРєРѕ Р°РґРјРёРЅР°Рј."

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
                    return f"{target.display_name} РїРѕР»СѓС‡РёР» 3/3 РІР°СЂРЅР° Рё РјСѓС‚ РЅР° 60 РјРёРЅСѓС‚. РџСЂРёС‡РёРЅР°: {reason or 'РЅРµ СѓРєР°Р·Р°РЅР°'}."
                except Exception as exc:
                    self._log("moderation_warn_mute_error", chat_id=chat_id, target=target.display_name, error=str(exc))
                    return f"{target.display_name} РїРѕР»СѓС‡РёР» РІР°СЂРЅ {warns}/{self.settings.warn_limit}, РЅРѕ РјСѓС‚ РЅРµ СЃСЂР°Р±РѕС‚Р°Р»: {exc}"
            self._log("moderation_warn", chat_id=chat_id, target=target.display_name, warns=warns, reason=reason)
            return f"{target.display_name} РїРѕР»СѓС‡РёР» РІР°СЂРЅ {warns}/{self.settings.warn_limit}. РџСЂРёС‡РёРЅР°: {reason or 'РЅРµ СѓРєР°Р·Р°РЅР°'}."

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
                return f"{target.display_name} Р·Р°РјСѓС‡РµРЅ РЅР° {minutes} РјРёРЅСѓС‚. РџСЂРёС‡РёРЅР°: {reason or 'РЅРµ СѓРєР°Р·Р°РЅР°'}."
            except Exception as exc:
                self._log("moderation_mute_error", chat_id=chat_id, target=target.display_name, error=str(exc))
                return f"РќРµ СЃРјРѕРіР»Р° РІС‹РґР°С‚СЊ РјСѓС‚: {exc}"

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
                return f"{target.display_name} СЂР°Р·РјСѓС‡РµРЅ."
            except Exception as exc:
                self._log("moderation_unmute_error", chat_id=chat_id, target=target.display_name, error=str(exc))
                return f"РќРµ СЃРјРѕРіР»Р° СЃРЅСЏС‚СЊ РјСѓС‚: {exc}"

        return "РќРµРёР·РІРµСЃС‚РЅРѕРµ РґРµР№СЃС‚РІРёРµ РјРѕРґРµСЂР°С†РёРё."

    async def _tool_create_poll(self, chat_id: int, args: dict[str, Any]) -> str:
        question = str(args.get("question") or "").strip()[:300]
        options = args.get("options") or []
        is_anonymous = bool(args.get("is_anonymous", True))
        allows_multiple_answers = bool(args.get("allows_multiple_answers", False))

        if not question:
            return "РќРµ СЃРјРѕРіР»Р° СЃРѕР·РґР°С‚СЊ РѕРїСЂРѕСЃ: РїСѓСЃС‚РѕР№ РІРѕРїСЂРѕСЃ."
        if not isinstance(options, list):
            return "РќРµ СЃРјРѕРіР»Р° СЃРѕР·РґР°С‚СЊ РѕРїСЂРѕСЃ: РІР°СЂРёР°РЅС‚С‹ РґРѕР»Р¶РЅС‹ Р±С‹С‚СЊ СЃРїРёСЃРєРѕРј."

        safe_options = [str(option).strip()[:100] for option in options if str(option).strip()]
        if len(safe_options) < 2:
            return "РќРµ СЃРјРѕРіР»Р° СЃРѕР·РґР°С‚СЊ РѕРїСЂРѕСЃ: РЅСѓР¶РЅРѕ РјРёРЅРёРјСѓРј 2 РІР°СЂРёР°РЅС‚Р°."

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
            return "РћРїСЂРѕСЃ СЃРѕР·РґР°РЅ."
        except Exception as exc:
            self._log("create_poll_error", chat_id=chat_id, error=str(exc), question=question)
            return f"РќРµ СЃРјРѕРіР»Р° СЃРѕР·РґР°С‚СЊ РѕРїСЂРѕСЃ: {exc}"

    def _resolve_target_user(self, chat_id: int, sender: Sender, target_name: str) -> ChatUser | None:
        normalized = (target_name or "").strip().lower()
        aliases = {"СЏ", "me", "РјРѕР№", "РјРЅРµ", sender.display_name.lower(), sender.first_name.lower()}
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
        if any(word in lowered for word in ["Р»СЋР±Р»СЋ", "РјРёР»Р°СЏ", "СѓРјРЅРёС†Р°", "СЃРѕР»РЅС‹С€РєРѕ", "С…РѕСЂРѕС€", "РєСЂР°СЃР°РІР°"]):
            delta += 2
        if any(word in lowered for word in ["Р±РµСЃРёС€СЊ", "РґСѓСЂР°Рє", "РёРґРёРѕС‚", "РЅР°С…СѓР№", "Р·Р°РµР±Р°Р»"]):
            delta -= 2
        self.moods[chat_id] = max(0, min(100, self.moods[chat_id] + delta))

    def _adjust_mood_from_user_message(self, chat_id: int, user_text: str) -> None:
        lowered = user_text.lower()
        delta = 0
        if any(word in lowered for word in ["СЃРїР°СЃРёР±Рѕ", "РѕР±РѕР¶Р°СЋ", "Р»СЋР±Р»СЋ", "СѓРјРЅРёС†Р°", "РєСЂР°СЃР°РІР°"]):
            delta += 4
        if any(word in lowered for word in ["С…СѓРµРІРѕ", "С…СѓС‘РІРѕ", "С‚СѓРї", "РіР»СѓРї", "РёРґРёРѕС‚", "РЅР°С…СѓР№", "РїРёР·Рґ", "РµР±Р°", "РїРѕС€Р»Р°"]):
            delta -= 6
        self.moods[chat_id] = max(0, min(100, self.moods[chat_id] + delta))

    def _finalize_reply(self, content: str, *, user_text: str) -> str:
        cleaned = content.strip()
        if not cleaned:
            return ""

        bot_name = re.escape(self.settings.bot_name)
        cleaned = re.sub(rf"^(?:{bot_name}\s*:\s*)+", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"^\s*РЅРµР№СЂРѕРЅРёРєР°\s*:\s*", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)

        if self._is_hostile_user_text(user_text):
            cleaned = re.sub(
                r"(?:\s*(?:Рђ|РќСѓ Р°|Р)\s+Сѓ\s+С‚РµР±СЏ\s+РєР°Рє.*|\s*РљР°Рє\s+Сѓ\s+С‚РµР±СЏ.*|\s*Р§РµРј\s+Р·Р°РЅСЏС‚.*|\s*Р§С‚Рѕ\s+РєРѕРЅРєСЂРµС‚РЅРѕ\s+РёРЅС‚РµСЂРµСЃСѓРµС‚\??)\s*$",
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
            "СЂР°РґРѕСЃС‚СЊ РјРѕСЏ",
            "Р·Р°СЏС†",
            "РґРµСЂР·РєРёР№ Р·Р°СЏС†",
            "РјР°РіРёСЏ",
            "СЃСЋСЂРїСЂРёР·Р°Рј",
            "СЃСЋСЂРїСЂРёР·С‹",
            "РјР°Р»РѕР№",
            "Р·РѕР»РѕС‚С†Рµ",
            "РјРёР»С‹Р№",
            "РјРёР»Р°СЏ",
        ]
        for phrase in soft_phrases:
            cleaned = re.sub(rf"\b{re.escape(phrase)}\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([,!.?])", r"\1", cleaned)
        return cleaned.strip(" ,")

    def _looks_too_soft(self, content: str) -> bool:
        lowered = content.lower()
        soft_markers = [
            "СЂР°РґРѕСЃС‚СЊ РјРѕСЏ",
            "Р·Р°СЏС†",
            "РјР°РіРёСЏ",
            "СЃСЋСЂРїСЂРёР·",
            "РЅРµ РѕСЃС‚Р°РІР»СЏС‚СЊ С‚РµР±СЏ СЂР°РІРЅРѕРґСѓС€РЅС‹Рј",
            "РїСЂРёРіРѕС‚РѕРІСЊСЃСЏ",
            "РєР°Рє С‚Р°Рј РґРµР»Р°",
        ]
        return any(marker in lowered for marker in soft_markers)

    def _is_hostile_user_text(self, user_text: str) -> bool:
        lowered = user_text.lower()
        hostile_tokens = [
            "С…СѓРµРІРѕ",
            "С…СѓС‘РІРѕ",
            "С‚СѓРї",
            "РіР»СѓРї",
            "РёРґРёРѕС‚",
            "РґСѓСЂР°",
            "РіР»СѓРїР°СЏ РіРѕР»РѕРІР°",
            "РЅР°С…СѓР№",
            "РїРёР·Рґ",
            "РµР±Р°",
            "РїРѕС€Р»Р°",
            "РѕС‚РІРµС‡Р°РµС€СЊ РєР°Рє-С‚Рѕ",
        ]
        return any(token in lowered for token in hostile_tokens)

