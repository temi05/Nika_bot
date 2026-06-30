from __future__ import annotations

import base64
import asyncio
import json
import random
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any

import httpx
from aiogram import Bot
from aiogram.types import ChatPermissions
from openai import AsyncOpenAI

from app.config import Settings
from app.models import ChatUser, Sender
from app.services.memory_provider import BaseMemoryProvider
from app.services.persona_service import PersonaService
from app.services.prompt_builders import build_character_system_prompt
from app.services.supabase_db import SupabaseDB

def retry_async(max_attempts=3, delay=1, backoff=2):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            last_exc = None
            current_delay = delay
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        raise last_exc
            return None
        return wrapper
    return decorator


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
        self.image_client = (
            AsyncOpenAI(
                api_key=settings.openai_api_key,
                base_url="https://api.openai.com/v1",
                timeout=settings.ai_timeout_seconds,
            )
            if settings.openai_api_key
            else self.client
        )
        self.chat_buffers: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=25))
        self.recent_bot_replies: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=8))
        self.moods: dict[int, int] = defaultdict(lambda: 60)
        self.last_group_reply_at: dict[int, datetime] = {}
        self.sticker_pack_name = "neuronics_pack_by_MoiStikiBot"
        self._sticker_cache: list[Any] = []

    def _log(self, event: str, **kwargs: Any) -> None:
        details = " ".join(f"{key}={value!r}" for key, value in kwargs.items())
        print(f"[AI:{event}] {details}".strip())

    def remember_message(self, chat_id: int, sender: Sender, text: str) -> None:
        rendered = self._render_chat_buffer_line(sender, text)
        self.chat_buffers[chat_id].append(rendered)
        self._log("remember", chat_id=chat_id, sender=sender.display_name, text=rendered[:160])

    def _render_chat_buffer_line(self, sender: Sender, text: str) -> str:
        # text - это то, что мы передаем в remember_message (ai_input_text)
        # Оно уже в XML формате. Просто возвращаем его, но убираем лишние переносы
        return re.sub(r"\n+", " ", text.strip())

    def _extract_reply_author(self, text: str) -> str | None:
        """Извлекает имя автора из <reply_target>, если оно там есть."""
        match = re.search(r'(?is)<reply_target[^>]*author="([^"]+)"', text)
        if match:
            author = match.group(1).strip()
            id_match = re.search(r'(?is)<reply_target[^>]*user_id="(\d+)"', text)
            if id_match:
                return f"{author} (user_id={id_match.group(1)})"
            return author
        return None

    def _summarize_for_history(self, text: str) -> str:
        plain = self._extract_current_plain_text(text)
        if not plain:
            plain = (text or "").strip()
        plain = re.sub(r"\s+", " ", plain).strip()
        return plain[:900] if plain else "[пустое сообщение]"

    @retry_async(max_attempts=3, delay=2)
    async def generate_cookie_gift_message(
        self,
        chat_id: int,
        sender_name: str,
        receiver_name: str,
        amount: int,
    ) -> str:
        """Генерирует умное сообщение при передаче печенек с помощью AI"""
        if not self.client:
            # Fallback если нет AI
            return self._fallback_cookie_message(sender_name, receiver_name, amount)

        # Получаем контекст из чата
        recent_messages = list(self.chat_buffers[chat_id])[-10:]
        context = "\n".join(recent_messages) if recent_messages else "Нет истории сообщений"

        prompt = f"""Ты - {self.settings.bot_name}, игривый и умный бот в Telegram группе.

Контекст последних сообщений в чате:
{context}

Пользователь {sender_name} передал {amount} печенек пользователю {receiver_name}.

Сгенерируй короткое (1-2 предложения), игривое и персонализированное сообщение о передаче печенек. 
Учитывай контекст чата и характер взаимодействия между пользователями.
Если есть что-то особенное в контексте - используй это.

Только сообщение, без описания действий."""

        try:
            response = await self.client.chat.completions.create(
                model=self.settings.ai_model,
                messages=[
                    {"role": "system", "content": "Ты игривый и умный бот. Генерируй только короткий ответ."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=100,
                temperature=0.8,
            )
            message = response.choices[0].message.content
            if message and message.strip():
                return message.strip()
        except Exception as e:
            self._log("cookie_gift_error", error=str(e))

        return self._fallback_cookie_message(sender_name, receiver_name, amount)

    def _fallback_cookie_message(self, sender_name: str, receiver_name: str, amount: int) -> str:
        """Генерирует простое сообщение без AI"""
        messages = [
            f"🍪 {sender_name} передал {amount} печенек {receiver_name}!",
            f"🎁 {sender_name} поощрил(а) {receiver_name} {amount} печенками!",
            f"✨ {sender_name} поделился(ась) печеньками с {receiver_name}!",
            f"💝 {sender_name} сделал(а) приятное {receiver_name} — {amount} печенек!",
        ]
        return random.choice(messages)

    async def flush_passive_memory(self, chat_id: int, *, force: bool = False) -> None:
        if len(self.chat_buffers[chat_id]) < (1 if force else 20):
            return

        transcript = "\n".join(self.chat_buffers[chat_id])
        participants = list(dict.fromkeys(line.split(":", 1)[0] for line in self.chat_buffers[chat_id]))
        self._log("flush_memory_start", chat_id=chat_id, participants=participants, lines=len(self.chat_buffers[chat_id]))
        await self.memory.save_transcript(chat_id, transcript, participants)
        self.chat_buffers[chat_id].clear()
        self._log("flush_memory_done", chat_id=chat_id)

    async def generate_image(self, prompt: str, reference_images: list[tuple[bytes, str]] | None = None) -> bytes | None:
        refs = reference_images or []
        if self.settings.polza_api_key and not self.settings.openai_api_key:
            return await self._generate_polza_media(prompt, refs)

        client = self.image_client or self.client
        if not client:
            return None
        clean_prompt = prompt.strip()
        if not clean_prompt:
            return None
        if refs and not self.settings.openai_api_key:
            self._log("image_reference_skip", reason="OPENAI_API_KEY_missing")
            refs = []
        try:
            if refs:
                image_files = []
                for index, (payload, mime_type) in enumerate(refs[:4], start=1):
                    suffix = "png"
                    if "jpeg" in mime_type or "jpg" in mime_type:
                        suffix = "jpg"
                    elif "webp" in mime_type:
                        suffix = "webp"
                    buffer = BytesIO(payload)
                    buffer.name = f"reference_{index}.{suffix}"
                    image_files.append(buffer)
                response = await client.images.edit(
                    model=self.settings.ai_image_model,
                    image=image_files,
                    prompt=clean_prompt[:3500],
                    size="1024x1024",
                )
            else:
                response = await client.images.generate(
                    model=self.settings.ai_image_model,
                    prompt=clean_prompt[:3500],
                    size="1024x1024",
                )
            return await self._read_image_response(response)
        except Exception as exc:
            self._log("image_error", used_reference=bool(refs), error=str(exc))
            if refs:
                try:
                    self._log("image_retry_without_reference")
                    response = await client.images.generate(
                        model=self.settings.ai_image_model,
                        prompt=clean_prompt[:3500],
                        size="1024x1024",
                    )
                    return await self._read_image_response(response)
                except Exception as retry_exc:
                    self._log("image_retry_error", error=str(retry_exc))
        return None

    async def _generate_polza_media(self, prompt: str, reference_images: list[tuple[bytes, str]]) -> bytes | None:
        clean_prompt = prompt.strip()
        if not clean_prompt or not self.settings.polza_api_key:
            return None

        model = self.settings.ai_image_model
        if model in {"gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini"}:
            model = f"openai/{model}"

        images = []
        for payload, mime_type in reference_images[:16]:
            encoded = base64.b64encode(payload).decode("ascii")
            images.append({"type": "base64", "data": f"data:{mime_type};base64,{encoded}"})

        headers = {
            "Authorization": f"Bearer {self.settings.polza_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "input": {
                "prompt": clean_prompt[:3500],
                "aspect_ratio": "1:1",
                "quality": "medium",
                "images": images,
                "output_format": "png",
                "max_images": 1,
            },
            "async": True,
        }

        try:
            async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
                created = await client.post("https://polza.ai/api/v1/media", headers=headers, json=body)
                created.raise_for_status()
                payload = created.json()
                media_id = payload.get("id")
                if not media_id:
                    return await self._read_polza_media_result(client, payload)
                for _ in range(45):
                    status_response = await client.get(f"https://polza.ai/api/v1/media/{media_id}", headers=headers)
                    status_response.raise_for_status()
                    status_payload = status_response.json()
                    status = status_payload.get("status")
                    if status == "completed":
                        return await self._read_polza_media_result(client, status_payload)
                    if status in {"failed", "cancelled"}:
                        self._log("polza_media_failed", status=status, error=status_payload.get("error"))
                        break
                    await asyncio.sleep(3)
        except Exception as exc:
            self._log("polza_media_error", used_reference=bool(images), error=str(exc))
            if images:
                try:
                    self._log("polza_media_retry_without_reference")
                    return await self._generate_polza_media(clean_prompt, [])
                except Exception as retry_exc:
                    self._log("polza_media_retry_error", error=str(retry_exc))
        return None

    async def _read_polza_media_result(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> bytes | None:
        image_url = self._extract_image_url(payload.get("data"))
        if not image_url:
            image_url = self._extract_image_url(payload)
        if not image_url:
            self._log("polza_media_no_url", keys=list(payload.keys()))
            return None
        downloaded = await client.get(image_url)
        downloaded.raise_for_status()
        return downloaded.content

    def _extract_image_url(self, value: Any) -> str | None:
        if isinstance(value, str):
            if value.startswith("http://") or value.startswith("https://"):
                return value
            return None
        if isinstance(value, dict):
            for key in ("url", "image_url", "output_url"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    return candidate
            for nested in value.values():
                found = self._extract_image_url(nested)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._extract_image_url(item)
                if found:
                    return found
        return None

    async def _read_image_response(self, response: Any) -> bytes | None:
        image = response.data[0] if getattr(response, "data", None) else None
        if not image:
            return None
        b64_json = getattr(image, "b64_json", None)
        if b64_json:
            return base64.b64decode(b64_json)
        image_url = getattr(image, "url", None)
        if image_url:
            async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
                download = await client.get(image_url)
                download.raise_for_status()
                return download.content
        return None

    async def generate_reply(
        self,
        chat_id: int,
        sender: Sender,
        user_text: str,
        reply_to_bot: bool,
        mentioned: bool,
        caller_is_admin: bool = False,
        is_private_chat: bool = False,
        image_data_urls: list[str] | None = None,
    ) -> str | None:
        if not user_text:
            self._log("skip", reason="empty_text", chat_id=chat_id)
            return None

        plain_user_text = self._extract_current_plain_text(user_text)
        
        # Сначала проверяем прямые команды, но даем на них живой ответ
        direct_reply = await self._maybe_handle_direct_action(chat_id, sender, plain_user_text)
        if direct_reply:
            self.remember_message(chat_id, Sender(user_id=0, first_name=self.settings.bot_name), direct_reply)
            self._remember_bot_reply(chat_id, direct_reply)
            self._adjust_mood(chat_id, direct_reply)
            self._mark_group_reply(chat_id, is_private_chat=is_private_chat)
            self._log("direct_action_reply", chat_id=chat_id, reply=direct_reply[:200])
            return direct_reply

        if not self.client or not self.settings.effective_ai_api_key:
            return "Связь с сервером барахлит, дай мне пару минут."

        if not self.settings.ai_model or not self.settings.effective_ai_base_url:
            return None

        addressed = mentioned or reply_to_bot
        is_media_message = self._is_media_marker(user_text)
        if not is_private_chat:
            if not addressed and is_media_message:
                return None
            if not addressed and len(plain_user_text.strip()) < self.settings.ai_min_message_len:
                return None
            if not addressed and self._group_reply_cooldown_active(chat_id):
                return None

        self._log(
            "start",
            chat_id=chat_id,
            sender=sender.display_name,
            reply_to_bot=reply_to_bot,
            mentioned=mentioned,
            caller_is_admin=caller_is_admin,
            user_text=plain_user_text[:200],
        )

        self.persona.observe_user_message(
            chat_id,
            sender.user_id,
            plain_user_text,
            reply_to_bot=reply_to_bot,
            mentioned=mentioned,
        )
        self._adjust_mood_from_user_message(chat_id, plain_user_text)
        persona_state = self.persona.bump_exchange(chat_id, sender.user_id)

        memory_text = await self.memory.get_relevant_facts(chat_id, plain_user_text, sender.display_name, sender.user_id)

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

        images = list(image_data_urls or [])
        history = self._history_without_current(chat_id, sender, user_text)
        context_text = self._build_generation_context(
            history=history,
            sender=sender,
            user_text=user_text,
            plain_user_text=plain_user_text,
            reply_to_bot=reply_to_bot,
            mentioned=mentioned,
            is_private_chat=is_private_chat,
        )
        # Combine the whole visible context into one user message to avoid consecutive
        # user roles, which causes BAD_REQUEST on some LLM providers.
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.append({"role": "user", "content": self._build_current_user_content(context_text, images)})

        try:
            use_tools = len(plain_user_text.strip()) >= 5 and not images
            for round_index in range(3):
                request_kwargs: dict[str, Any] = {
                    "model": self.settings.ai_model,
                    "messages": messages,
                    "temperature": self.settings.ai_temperature,
                    "max_tokens": self.settings.ai_max_tokens,
                }
                if use_tools:
                    request_kwargs["tools"] = self._tool_definitions()
                    request_kwargs["tool_choice"] = "auto"
                
                response = await self._chat_completion(**request_kwargs)
                message = response.choices[0].message
                tool_calls = list(message.tool_calls or [])
                self._log(
                    "model_response",
                    chat_id=chat_id,
                    round=round_index + 1,
                    model=self.settings.ai_model,
                    finish_reason=getattr(response.choices[0], "finish_reason", None),
                    content_type=type(message.content).__name__,
                    content_len=len(message.content) if isinstance(message.content, str) else 0,
                    tool_calls=len(tool_calls),
                )

                if tool_calls:
                    if not self._tool_calls_have_valid_json(tool_calls):
                        return self._fallback_for_unclear_input(plain_user_text)

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
                    continue

                raw_text = self._coerce_model_content(message.content)
                
                # Если ИИ вызвал инструмент и не выдал текста - это нормально, не считаем это ошибкой
                if not raw_text and tool_calls:
                    self._log("only_tool_reply", chat_id=chat_id, tools=len(tool_calls))
                    # Помечаем, что мы ответили (чтобы кулдаун работал)
                    self._mark_group_reply(chat_id, is_private_chat=is_private_chat)
                    return "" # Возвращаем пустую строку, обработчик в messages.py должен это понять как "успех без текста"

                content = self._finalize_reply(raw_text, user_text=user_text)
                
                if not content:
                    rescued = await self._retry_empty_reply(messages, sender.display_name, user_text)
                    if rescued:
                        rescued = await self._ensure_non_repetitive_reply(
                            chat_id=chat_id, messages=messages, sender_name=sender.display_name, user_text=plain_user_text, content=rescued
                        )
                        self._save_and_mark_final_reply(chat_id, sender, rescued, is_private_chat)
                        return rescued
                    return None

                content = await self._ensure_non_repetitive_reply(
                    chat_id=chat_id, messages=messages, sender_name=sender.display_name, user_text=plain_user_text, content=content
                )
                
                # Печеньки могут выдаваться через Tool, но здесь страхуем спонтанные
                content = self._maybe_attach_cookie_reward(chat_id, sender, plain_user_text, content)
                
                self._save_and_mark_final_reply(chat_id, sender, content, is_private_chat)
                self._log("final_reply_ready", chat_id=chat_id, length=len(content))
                return content

        except Exception as exc:
            self._log("error", chat_id=chat_id, error=str(exc))
            if images:
                fallback = await self._retry_text_only_after_vision_error(messages=messages, user_text=plain_user_text, error=str(exc))
                if fallback:
                    self._save_and_mark_final_reply(chat_id, sender, fallback, is_private_chat)
                    return fallback
            return "Ой, что-то в голове замкнуло... Давай попробуем еще раз через минутку? 😵"
            
        self._log("no_reply_generated", chat_id=chat_id)
        return "Я немного запуталась в мыслях. Давай конкретнее, а то я не знаю что ответить."

    def _history_without_current(self, chat_id: int, sender: Sender, user_text: str) -> list[str]:
        history = self._recent_persistent_history(chat_id, user_text)
        history.extend(self.chat_buffers[chat_id])

        current_line = self._render_chat_buffer_line(sender, user_text)
        if history and history[-1] == current_line:
            history = history[:-1]

        limit = max(0, self.settings.ai_history_lines)
        if limit <= 0:
            return []

        deduped: list[str] = []
        seen: set[str] = set()
        for line in history:
            key = self._normalize_reply_key(line)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(line)
        return deduped[-limit:]

    def _render_history_with_reply_chains(self, history: list[str]) -> str:
        """Возвращает историю как есть — reply_to уже раскрыты в supabase_db."""
        return "\n".join(history)

    def _recent_persistent_history(self, chat_id: int, user_text: str) -> list[str]:
        if not self.settings.ai_persistent_history_enabled:
            return []
        current_message_id = self._extract_current_message_id(user_text)
        try:
            return self.db.get_recent_message_context(
                chat_id,
                limit=self.settings.ai_persistent_history_lines,
                exclude_message_id=current_message_id,
            )
        except Exception as exc:
            self._log("persistent_history_error", chat_id=chat_id, error=str(exc))
            return []

    def _extract_current_message_id(self, user_text: str) -> int | None:
        match = re.search(r'(?i)<msg[^>]*id="(\d+)"', user_text)
        if match:
            return int(match.group(1))
        return None

    def _build_generation_context(
        self,
        *,
        history: list[str],
        sender: Sender,
        user_text: str,
        plain_user_text: str,
        reply_to_bot: bool,
        mentioned: bool,
        is_private_chat: bool,
    ) -> str:
        history_block = self._render_history_with_reply_chains(history) if history else "Недавней истории нет."
        addressed_to_bot = is_private_chat or reply_to_bot or mentioned

        return "\n".join(
            [
                "<dialogue_input>",
                "<attribution_rules>",
                "- ВАЖНО: Диалог размечен тегами <msg>.",
                "- Атрибуты: author (имя), user_id, type (тип), reply_to (на кого ответили), forwarded.",
                "- Если есть блок <reply_target>, текущее сообщение (<current_message>) — это ПРЯМОЙ ОТВЕТ на него.",
                "- Твой ответ должен быть логически связан с текстом в <reply_target>, если он есть.",
                "- Не путай отправителя и автора цитаты в <reply_target>.",
                "</attribution_rules>",
                "<recent_messages>",
                history_block,
                "</recent_messages>",
                "<current_request>",
                f"sender_name: {sender.display_name}",
                f"sender_user_id: {sender.user_id}",
                f"addressed_to_bot: {addressed_to_bot}",
                f"reply_to_bot: {reply_to_bot}",
                f"mentioned_bot: {mentioned}",
                f"is_reply: {'yes' if '<reply_target>' in user_text else 'no'}",
                "message_payload:",
                user_text,
                "</current_request>",
                "</dialogue_input>",
            ]
        )

    def _save_and_mark_final_reply(self, chat_id: int, sender: Sender, content: str, is_private_chat: bool) -> None:
        self.remember_message(chat_id, Sender(user_id=0, first_name=self.settings.bot_name), content)
        self._remember_bot_reply(chat_id, content)
        self._adjust_mood(chat_id, content)
        self._mark_group_reply(chat_id, is_private_chat=is_private_chat)
        self._log("final_reply", chat_id=chat_id, reply=content[:240], mood=self.moods[chat_id])

    async def _maybe_handle_direct_action(self, chat_id: int, sender: Sender, user_text: str) -> str | None:
        memory_text = self._extract_memory_request(user_text)
        if memory_text:
            user = self.db.get_or_create_user(chat_id, sender)
            self.db.append_ai_note(user, memory_text)
            self._log("direct_memory_saved", chat_id=chat_id, sender=sender.display_name, text=memory_text[:160])
            return f"Окей, отложила в памяти: {memory_text}"

        poll_request = self._extract_poll_request(user_text)
        if not poll_request:
            return None

        result = await self._tool_create_poll(chat_id, poll_request)
        if "Опрос создан" in result:
            return "Закинула опрос. Давайте, голосуйте."
        return f"Опрос не получился: {result}"

    def _extract_memory_request(self, user_text: str) -> str | None:
        text = user_text.strip()
        if not text:
            return None

        bot_names = [self.settings.bot_name]
        if self.settings.bot_username:
            bot_names.append(f"@{self.settings.bot_username}")
        for name in filter(None, bot_names):
            text = re.sub(rf"^\s*{re.escape(name)}\s*[,;:\-–—]?\s*", "", text, flags=re.IGNORECASE)

        match = re.search(r"(?is)\b(?:запомни|запиши|сохрани|remember)\b\s*(?:,?\s*(?:что|это|себе|that))?\s*[:\-–—]?\s*(.+)", text)
        if not match:
            return None

        memory_text = re.sub(r"\s+", " ", match.group(1)).strip(" .")
        return memory_text[:300] if len(memory_text) >= 4 else None

    def _extract_poll_request(self, user_text: str) -> dict[str, Any] | None:
        text = re.sub(r"\s+", " ", (user_text or "").strip())
        if not text or "<" in text or ">" in text:
            return None

        bot_names = [self.settings.bot_name]
        if self.settings.bot_username:
            bot_names.append(f"@{self.settings.bot_username}")
        for name in filter(None, bot_names):
            text = re.sub(rf"^\s*{re.escape(name)}\s*[,;:\-–—]?\s*", "", text, flags=re.IGNORECASE)

        poll_words = {"опрос", "голосование", "poll"}
        lead_words = {"сделай", "создай", "запусти", "устрой", "делаем", "make", "create", "start"}
        words = [word.strip(" ,.:;!?-–—").casefold() for word in text.split()]
        if not words:
            return None
        poll_index = 1 if words[0] in lead_words else 0
        if poll_index >= len(words) or words[poll_index] not in poll_words:
            return None

        lowered = text.lower()
        body = text.split(":", 1)[1].strip() if ":" in text else " ".join(text.split()[poll_index + 1 :]).strip()
        body = body.strip(" .,-")

        options: list[str] = []
        if "," in body:
            options = [part.strip() for part in body.split(",") if part.strip()]
        elif ";" in body:
            options = [part.strip() for part in body.split(";") if part.strip()]
        else:
            or_pattern = r"(?i)\s+или\s+"
            if re.search(or_pattern, body):
                options = [part.strip() for part in re.split(or_pattern, body) if part.strip()]

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
                    "description": "Искать профиль по имени, @username, user_id или ключевым словам. Нечёткий поиск: Dasha=Даша. action=profile — полный профиль, action=search — список.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["profile", "search"]},
                            "query": {"type": "string", "description": "Имя, @username, user_id или ключевое слово"},
                            "user_id": {"type": "integer", "description": "Числовой user_id для точного поиска (приоритет над query)"},
                        },
                        "required": ["action", "query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "manage_user_profile",
                    "description": "Обновить описание (bio) или добавить заметку о пользователе. target_name — ТОТ, О КОМ факт. Если есть target_user_id — используй для точности.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "target_name": {"type": "string", "description": "Имя пользователя (нечёткий поиск)"},
                            "target_user_id": {"type": "integer", "description": "Числовой user_id цели (предпочтительнее имени)"},
                            "action": {"type": "string", "enum": ["update_bio", "add_note"]},
                            "content": {"type": "string", "description": "Факт или описание"},
                        },
                        "required": ["target_name", "action", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "moderate_user",
                    "description": "Наградить (reward), предупредить (warn) или замутить (mute). Цель — ТОЛЬКО явный автор текущего действия. Если есть target_user_id — используй.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "target_name": {"type": "string", "description": "Имя нарушителя или отличившегося"},
                            "target_user_id": {"type": "integer", "description": "Числовой user_id цели (предпочтительнее имени)"},
                            "action": {"type": "string", "enum": ["warn", "mute", "unmute", "reward"]},
                            "value": {"type": "number", "description": "Количество печенек (1-3) или минут мута"},
                            "reason": {"type": "string", "description": "За что выдано (для мута/варна)"},
                        },
                        "required": ["target_name", "action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_poll",
                    "description": (
                        "Создать реальный опрос в Telegram чате. ВЫЗЫВАЙ ЭТОТ ИНСТРУМЕНТ ВСЕГДА, когда пользователь просит опрос, голосование или предоставляет варианты выбора. "
                        "Никогда не пиши варианты просто текстом."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "Сам вопрос для голосования"},
                            "options": {"type": "array", "items": {"type": "string"}, "description": "Массив вариантов ответа (минимум 2)"},
                            "is_anonymous": {"type": "boolean", "description": "Обычно true"},
                            "allows_multiple_answers": {"type": "boolean", "description": "Обычно false"},
                        },
                        "required": ["question", "options"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_reaction",
                    "description": "Поставить эмодзи-реакцию на сообщение пользователя. Используй для выражения эмоций (смех, лайк, огонь и т.д.).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "emoji": {"type": "string", "description": "Один эмодзи для реакции (👍, ❤️, 🔥, 😂, 😮, 😢, 👏, 🌭, 🤡 и др.)"},
                            "message_id": {"type": "integer", "description": "ID сообщения, на которое ставим реакцию. Обычно текущее."},
                        },
                        "required": ["emoji"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_visual",
                    "description": "Сгенерировать и отправить изображение в чат. Используй, когда хочешь что-то показать или визуализировать свою мысль.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "description": "Детальное описание того, что нужно нарисовать на английском языке."},
                        },
                        "required": ["prompt"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_chat_top",
                    "description": "Посмотреть топ игроков чата по уровню (xp) или печенькам (reputation).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string", "enum": ["xp", "reputation"], "description": "Категория топа"},
                        },
                        "required": ["category"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_dice",
                    "description": "Отправить анимированный кубик, слот-машину, футбольный мяч и т.д. в чат. Используй для фана или решения споров.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "emoji": {"type": "string", "enum": ["🎲", "🎯", "🏀", "⚽", "🎳", "🎰"], "description": "Тип анимированного эмодзи (по умолчанию 🎲)"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "transfer_reputation",
                    "description": "Передать печеньки (репутацию) от одного пользователя другому. Вызывай, когда пользователь просит Нику дать, перевести или подарить печеньки кому-то.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "target_name": {"type": "string", "description": "Имя получателя или @username"},
                            "amount": {"type": "integer", "description": "Количество печенек"},
                        },
                        "required": ["target_name", "amount"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remember_user_fact",
                    "description": "Запомнить важный факт о пользователе (имя, хобби, предпочтения, день рождения и т.д.) в базу данных для долговременной памяти.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "target_name": {"type": "string", "description": "Имя пользователя или @username, о ком факт"},
                            "fact": {"type": "string", "description": "Сам факт (например: 'Обожает играть в Доту', 'День рождения 15 сентября')"}
                        },
                        "required": ["target_name", "fact"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "send_sticker",
                    "description": "Отправить стикер из своего пака в чат для выражения эмоции (сарказм, смех, гнев, умиление и т.д.).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "emoji_hint": {
                                "type": "string",
                                "description": "Смысловой эмодзи (например: 😂, 🤡, 😎, 😡), чтобы подобрать подходящий стикер."
                            }
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "flip_coin_decide",
                    "description": "Бросить монетку для принятия случайного решения (Орел/Решка, Да/Нет). Используй для разрешения споров или когда просят случайный выбор.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "Суть спора или вопрос, на который бросается монетка (необязательно)."
                            }
                        }
                    }
                }
            },
        ]

    async def _run_tool_call(self, chat_id: int, sender: Sender, caller_is_admin: bool, tool_name: str, raw_arguments: str) -> str:
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return "Системная ошибка: не смогла разобрать аргументы."

        if tool_name == "remember_user_fact":
            args["action"] = "add_note"
            args["content"] = args.pop("fact", "")
            return self._tool_manage_user_profile(chat_id, sender, args)
        if tool_name == "send_sticker":
            return await self._tool_send_sticker(chat_id, args)
        if tool_name == "flip_coin_decide":
            return await self._tool_flip_coin_decide(chat_id, args)
        if tool_name == "user_lookup":
            return self._tool_user_lookup(chat_id, sender, args)
        if tool_name == "manage_user_profile":
            return self._tool_manage_user_profile(chat_id, sender, args)
        if tool_name == "moderate_user":
            return await self._tool_moderate_user(chat_id, caller_is_admin, args)
        if tool_name == "create_poll":
            return await self._tool_create_poll(chat_id, args)
        if tool_name == "transfer_reputation":
            return await self._tool_transfer_reputation(chat_id, sender, args)
        if tool_name == "send_reaction":
            return await self._tool_send_reaction(chat_id, args)
        if tool_name == "generate_visual":
            return await self._tool_generate_visual(chat_id, args)
        if tool_name == "get_chat_top":
            return self._tool_get_chat_top(chat_id, args)
        if tool_name == "send_dice":
            return await self._tool_send_dice(chat_id, args)
        return "Неизвестный инструмент."

    async def _tool_send_reaction(self, chat_id: int, args: dict[str, Any]) -> str:
        emoji = str(args.get("emoji") or "").strip()
        msg_id = args.get("message_id")
        
        if not emoji:
            return "Реакция не поставлена: не указан эмодзи."
            
        try:
            from aiogram.types import ReactionTypeEmoji
            await self.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=msg_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)]
            )
            return f"Реакция {emoji} успешно поставлена."
        except Exception as exc:
            self._log("reaction_error", error=str(exc))
            return f"Не удалось поставить реакцию: {exc}"

    async def _tool_generate_visual(self, chat_id: int, args: dict[str, Any]) -> str:
        mood = self.moods.get(chat_id, 60)
        if mood < 35:
            return f"Отклонено: У Ники отвратительное настроение ({mood}/100), она наотрез отказывается рисовать для вас или выполнять ваши капризы. Пусть её сначала кто-то задобрит теплыми словами или печеньками!"

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return "Не указан промпт для рисования."
            
        image_bytes = await self.generate_image(prompt)
        if not image_bytes:
            return "Не удалось сгенерировать изображение."
            
        try:
            from aiogram.types import BufferedInputFile
            await self.bot.send_photo(
                chat_id=chat_id,
                photo=BufferedInputFile(image_bytes, filename="nika_art.png"),
                caption=f"🎨 Нарисовала специально для вас по запросу: {prompt[:100]}..."
            )
            return "Изображение успешно сгенерировано и отправлено в чат."
        except Exception as exc:
            return f"Ошибка при отправке изображения: {exc}"

    def _tool_get_chat_top(self, chat_id: int, args: dict[str, Any]) -> str:
        category = str(args.get("category") or "xp").lower()
        users = self.db.get_top_users(chat_id, limit=5, order_by=category)
        
        if not users:
            return "В чате пока нет данных для топа."
            
        lines = [f"Топ-5 по {category}:"]
        for idx, u in enumerate(users, start=1):
            val = u.level if category == "xp" else u.reputation
            lines.append(f"{idx}. {u.display_name} — {val}")
        return "\n".join(lines)

    async def _tool_send_dice(self, chat_id: int, args: dict[str, Any]) -> str:
        emoji = str(args.get("emoji") or "🎲")
        try:
            await self.bot.send_dice(chat_id=chat_id, emoji=emoji)
            return f"Анимированный эмодзи {emoji} отправлен."
        except Exception as exc:
            return f"Ошибка при отправке кубика: {exc}"

    async def _tool_flip_coin_decide(self, chat_id: int, args: dict[str, Any]) -> str:
        question = str(args.get("question") or "").strip()
        result = random.choice(["Орёл 🦅", "Решка 🪙"])
        decision = random.choice(["ДА, однозначно!", "НЕТ, забудь об этом."])
        
        response_text = f"🪙 <b>Бросаю монетку...</b>\n"
        if question:
            response_text += f"Вопрос: <i>«{question}»</i>\n"
        response_text += f"Результат: <b>{result}</b>\nРешение: <b>{decision}</b>"
        
        try:
            await self.bot.send_message(chat_id=chat_id, text=response_text, parse_mode="HTML")
            return f"Монетка брошена. Результат: {result}, решение: {decision}."
        except Exception as exc:
            return f"Не удалось бросить монетку: {exc}"

    async def _tool_send_sticker(self, chat_id: int, args: dict[str, Any]) -> str:
        emoji_hint = str(args.get("emoji_hint") or "").strip()
        
        try:
            # Обновляем кэш стикеров, если он пуст
            if not self._sticker_cache:
                sticker_set = await self.bot.get_sticker_set(self.sticker_pack_name)
                self._sticker_cache = sticker_set.stickers

            if not self._sticker_cache:
                return "В стикерпаке нет стикеров."

            # Ищем стикер по эмодзи
            target_sticker = None
            if emoji_hint:
                # Пытаемся найти точное совпадение или вхождение эмодзи
                for s in self._sticker_cache:
                    if emoji_hint in (s.emoji or ""):
                        target_sticker = s
                        break
            
            # Если не нашли по эмодзи, берем случайный
            if not target_sticker:
                target_sticker = random.choice(self._sticker_cache)

            await self.bot.send_sticker(chat_id=chat_id, sticker=target_sticker.file_id)
            return f"Стикер {target_sticker.emoji} из пака {self.sticker_pack_name} отправлен."
        except Exception as exc:
            self._log("sticker_error", error=str(exc))
            return f"Не удалось отправить стикер: {exc}"

    def _tool_calls_have_valid_json(self, tool_calls: list[Any]) -> bool:
        for call in tool_calls:
            try:
                json.loads(call.function.arguments or "{}")
            except (AttributeError, json.JSONDecodeError, TypeError):
                return False
        return True

    def _fallback_for_unclear_input(self, user_text: str) -> str:
        if len(user_text.strip()) <= 5:
            return "Сформулируй мысль полнее, я по одному слову не гадаю."
        return "Не поняла, чего ты хочешь. Давай конкретнее."

    def _build_current_user_content(self, text: str, image_data_urls: list[str]) -> str | list[dict[str, Any]]:
        if not image_data_urls:
            return text
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for data_url in image_data_urls:
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        return content

    def _strip_images_from_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned_messages: list[dict[str, Any]] = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                text_parts: list[str] = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            text_parts.append(text.strip())
                cleaned_messages.append({**message, "content": "\n".join(text_parts)})
            else:
                cleaned_messages.append(message)
        return cleaned_messages

    async def _retry_text_only_after_vision_error(self, *, messages: list[dict[str, Any]], user_text: str, error: str) -> str:
        try:
            response = await self._chat_completion(
                model=self.settings.ai_model,
                messages=self._strip_images_from_messages(messages),
                temperature=self.settings.ai_temperature,
                max_tokens=self.settings.ai_max_tokens,
            )
            message = response.choices[0].message
            raw_text = self._coerce_model_content(message.content)
            return self._finalize_reply(raw_text, user_text=user_text)
        except Exception:
            return ""

    def _tool_user_lookup(self, chat_id: int, sender: Sender, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        query = str(args.get("query") or "").strip()
        explicit_uid = args.get("user_id")

        if explicit_uid:
            try:
                target = self.db.get_user_by_platform_id(chat_id, int(explicit_uid))
            except (TypeError, ValueError):
                target = None
            if target:
                return self._format_user_profile_tool(chat_id, target)
            return f"Не нашла пользователя с user_id={explicit_uid}."

        if not query:
            return "Пустой запрос для поиска."

        if action == "search":
            users = self.db.get_all_users(chat_id)
            normalized = query.lower()
            matches: list[str] = []
            for user in users:
                haystack = " | ".join(filter(None, [
                    user.display_name.lower(),
                    (f"@{user.username}".lower() if user.username else ""),
                    str(user.user_id),
                    (user.bio or "").lower(),
                    (user.ai_notes or "").lower(),
                ]))
                if normalized in haystack:
                    uname = f" (@{user.username})" if user.username else ""
                    matches.append(f"{user.display_name}{uname} [id={user.user_id}]")
            if not matches:
                found = self.db.search_user(chat_id, query)
                if found:
                    uname = f" (@{found.username})" if found.username else ""
                    matches.append(f"{found.display_name}{uname} [id={found.user_id}]")
            if not matches:
                return "В базе таких нет."
            return "Нашла совпадения:\n" + "\n".join(f"- {name}" for name in matches[:10])

        target = self._resolve_target_user(chat_id, sender, query)
        if not target:
            return f"Не нашла пользователя '{query}'. Попробуй @username или user_id."
        return self._format_user_profile_tool(chat_id, target)

    def _format_user_profile_tool(self, chat_id: int, target: ChatUser) -> str:
        facts = self.db.get_user_facts_by_id(chat_id, target.user_id, limit=6)
        if not facts:
            facts = self.db.get_all_user_facts(chat_id, target.display_name, limit=6)
        uname = f" | @{target.username}" if getattr(target, "username", None) else ""
        lines = [
            f"Профиль: {target.display_name}{uname} (user_id={target.user_id})",
            f"Уровень: {target.level} (XP: {target.xp})",
            f"Печеньки: {target.reputation}",
            f"Варны: {target.warns}/{self.settings.warn_limit}",
            f"Био: {target.bio or 'Пусто'}",
            f"Заметки: {target.ai_notes or 'Нет'}",
        ]
        if facts:
            lines.append("Факты из базы:")
            lines.extend(f"- {fact}" for fact in facts[:4])
        return "\n".join(lines)

    def _tool_manage_user_profile(self, chat_id: int, sender: Sender, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        target_name = str(args.get("target_name") or "").strip()
        explicit_uid = args.get("target_user_id")
        content = str(args.get("content") or "").strip()
        if not content:
            return "Нечего сохранять, пустой текст."

        target = None
        if explicit_uid:
            try:
                target = self.db.get_user_by_platform_id(chat_id, int(explicit_uid))
            except (TypeError, ValueError):
                pass
        if not target:
            target = self._resolve_target_user(chat_id, sender, target_name)
        if not target:
            return f"Не нашла пользователя '{target_name}' для записи."

        if action == "update_bio":
            self.db.set_bio(target, content)
            return f"Био для {target.display_name} успешно обновлено."
        if action == "add_note":
            self.db.append_ai_note(target, content)
            return f"Факт о {target.display_name} сохранен в базу."
        return "Неизвестное действие с профилем."

    async def _tool_moderate_user(self, chat_id: int, caller_is_admin: bool, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").lower()
        target_name = str(args.get("target_name") or "").strip()
        explicit_uid = args.get("target_user_id")
        reason = str(args.get("reason") or "").strip()
        value = int(args.get("value") or 0)

        if not target_name and not explicit_uid:
            return "Не указано, к кому применять действие."

        target = None
        if explicit_uid:
            try:
                target = self.db.get_user_by_platform_id(chat_id, int(explicit_uid))
            except (TypeError, ValueError):
                pass
        if not target:
            target = self.db.search_user(chat_id, target_name)
        if not target:
            return f"Не вижу пользователя '{target_name}'."

        if action == "reward":
            amount = min(max(value or 1, 1), 3)
            self.db.update_user(target.id, {"reputation": target.reputation + amount})
            return f"Выдано {amount} печенек для {target.display_name}."

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
                    return f"Пользователь {target.display_name} достиг лимита варнов и получил мут на час. Причина: {reason}"
                except Exception as exc:
                    return f"Варн выдан, но выдать мут не удалось (нет прав в чате?): {exc}"
            return f"Варн {warns}/{self.settings.warn_limit} выдан пользователю {target.display_name}. Причина: {reason}"

        if action == "mute":
            minutes = min(max(value or 15, 1), 1440)
            try:
                await self.bot.restrict_chat_member(
                    chat_id,
                    target.user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=datetime.now() + timedelta(minutes=minutes),
                )
                return f"Мут на {minutes} минут выдан {target.display_name}. Причина: {reason}"
            except Exception as exc:
                return f"Ошибка выдачи мута: {exc}"

        if action == "unmute":
            try:
                await self.bot.restrict_chat_member(
                    chat_id,
                    target.user_id,
                    permissions=ChatPermissions(
                        can_send_messages=True, can_send_audios=True, can_send_documents=True,
                        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                        can_add_web_page_previews=True, can_invite_users=True,
                    ),
                )
                return f"Мут снят с {target.display_name}."
            except Exception as exc:
                return f"Ошибка снятия мута: {exc}"

        return "Неизвестное действие."

    async def _tool_create_poll(self, chat_id: int, args: dict[str, Any]) -> str:
        question = str(args.get("question") or "").strip()[:300]
        options = args.get("options") or []
        is_anonymous = bool(args.get("is_anonymous", True))
        allows_multiple_answers = bool(args.get("allows_multiple_answers", False))

        if not question:
            return "Опрос не создан: нужен вопрос."
        if not isinstance(options, list):
            return "Опрос не создан: варианты должны быть списком."

        safe_options = self._sanitize_poll_options(options)
        if len(safe_options) < 2:
            return "Опрос не создан: укажи хотя бы два нормальных варианта."
        if self._looks_like_bad_poll(question, safe_options):
            return "Опрос отменен: варианты похожи на системный мусор."

        try:
            sent_msg = await self.bot.send_poll(
                chat_id=chat_id,
                question=question,
                options=safe_options[:10],
                is_anonymous=is_anonymous,
                allows_multiple_answers=allows_multiple_answers,
            )
            # Регистрируем опрос, чтобы отслеживать голоса
            if sent_msg.poll:
                self.db.register_poll(
                    poll_id=sent_msg.poll.id,
                    chat_id=chat_id,
                    question=question,
                    options=safe_options[:10]
                )
            return "Опрос успешно создан и отправлен в чат."
        except Exception as exc:
            return f"Ошибка API Telegram при создании опроса: {exc}"

    async def _tool_transfer_reputation(self, chat_id: int, sender: Sender, args: dict[str, Any]) -> str:
        mood = self.moods.get(chat_id, 60)
        if mood < 35:
            return f"Отклонено: Ника слишком раздражена ({mood}/100), чтобы пересылать печеньки по вашему приказу. Она советует сначала её порадовать!"

        target_name = str(args.get("target_name") or "").strip()
        amount = int(args.get("amount") or 0)
        
        if amount <= 0:
            return "Ошибка: количество печенек должно быть больше нуля."
            
        target = self._resolve_target_user(chat_id, sender, target_name)
        if not target:
            return f"Не нашла пользователя '{target_name}'."
            
        if target.user_id == sender.user_id:
            return "Нельзя переводить печеньки самому себе."
            
        sender_db = self.db.get_or_create_user(chat_id, sender)
        if sender_db.reputation < amount:
            return f"У тебя недостаточно печенек. Баланс: {sender_db.reputation}."
            
        # Выполняем транзакцию
        self.db.update_user(sender_db.id, {"reputation": sender_db.reputation - amount})
        self.db.update_user(target.id, {"reputation": target.reputation + amount})
        
        # Генерируем живой ответ через ИИ
        ai_msg = await self.generate_cookie_gift_message(chat_id, sender.display_name, target.display_name, amount)
        return f"ТРАНЗАКЦИЯ ВЫПОЛНЕНА: {ai_msg}"

    def _sanitize_poll_options(self, options: list[Any]) -> list[str]:
        safe_options: list[str] = []
        seen: set[str] = set()
        for option in options:
            value = re.sub(r"\s+", " ", str(option or "")).strip(" .,-:;")
            if not value:
                continue
            value = value[:100]
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            safe_options.append(value)
        return safe_options[:10]

    def _looks_like_bad_poll(self, question: str, options: list[str]) -> bool:
        normalized_question = re.sub(r"\s+", " ", question.strip()).casefold()
        if self._looks_like_memory_artifact(normalized_question):
            return True

        bad_markers = (
            "<reply_target",
            "<msg",
            "</msg>",
            "reply_to=",
            "author=",
            "user_id=",
            "type=",
        )
        joined = " ".join(options).casefold()
        if any(marker in joined for marker in bad_markers):
            return True

        long_options = sum(1 for option in options if len(option) > 60)
        sentence_options = sum(1 for option in options if re.search(r"[.!?].{8,}", option))
        if long_options >= 2 or sentence_options >= 1:
            return True

        return False

    def _maybe_attach_cookie_reward(self, chat_id: int, sender: Sender, user_text: str, reply: str) -> str:
        # Проверяем, не выпрашивает ли человек печеньку намеренно
        lowered_req = user_text.lower()
        if "дай печеньку" in lowered_req or "хочу печеньку" in lowered_req:
            return reply

        # СНАЧАЛА ПРОВЕРЯЕМ НА ГРУБОСТЬ (Умный штраф)
        if self._is_hostile_user_text(user_text):
            # Проверяем кулдаун на штрафы (300 секунд), чтобы не списать всё сразу
            if self.db.can_adjust_reputation(0, sender.user_id, cooldown_seconds=300):
                user = self.db.get_or_create_user(chat_id, sender)
                if user.reputation > 0:
                    self.db.update_user(user.id, {"reputation": max(0, user.reputation - 1)})
                    # Снижаем настроение Ники
                    self.moods[chat_id] = max(0, self.moods[chat_id] - 15)
                    return f"{reply}\n\n💢 <b>-1 🍪 за грубость.</b> Не смей мне хамить!"

        # Если не грубил, проверяем на заслуженную награду
        if not self._message_deserves_cookie(user_text):
            return reply
        if not self.db.can_adjust_reputation(0, sender.user_id, cooldown_seconds=180):
            return reply

        user = self.db.get_or_create_user(chat_id, sender)
        updated = self.db.update_user(user.id, {"reputation": user.reputation + 1})
        if not updated:
            return reply

        if "🍪" in reply or "печен" in reply.casefold():
            return reply
        return f"{reply}\n\n🍪 Держи печеньку, это было хорошо."

    def _message_deserves_cookie(self, user_text: str) -> bool:
        text = re.sub(r"\s+", " ", (user_text or "").strip()).casefold()
        if len(text) < 3:
            return False
            
        # Список стоп-слов, которые отменяют награду (отрицание или токсичность)
        negation_markers = {"не ", "ни разу", "вряд ли", "хуже", "плохо", "отстой", "удали", "бесишь", "тупая", "дура"}
        if any(marker in text for marker in negation_markers):
            return False

        # Основные позитивные маркеры
        positive_markers = {
            "спасибо", "спс", "красава", "харош", "хорош", "база",
            "гениально", "смешно", "угар", "топ", "сильно", "вайб",
            "умница", "лучшая", "обожаю", "люблю", "мило", "круто"
        }
        
        # Проверяем наличие слов (целиком, чтобы не ловить "стоп" в "растоптать")
        words = set(re.findall(r"\b\w+\b", text))
        if any(marker in words for marker in positive_markers):
            # Если сообщение очень короткое (типа просто "спс"), даем шанс 30%
            if len(text) < 10:
                return random.random() < 0.3
            return True
            
        # Эмодзи тоже считаются
        emoji_pattern = r"(^|\s)(\+1|\+\+|👍|🔥|❤️|😂|🤣|🥰|💎|👑)($|\s)"
        return bool(re.search(emoji_pattern, text))

    def _resolve_target_user(self, chat_id: int, sender: Sender, target_name: str) -> ChatUser | None:
        normalized = (target_name or "").strip().lower()
        if not normalized:
            return None

        # Поиск по числовому user_id
        if re.fullmatch(r"-?\d+", normalized):
            found = self.db.get_user_by_platform_id(chat_id, int(normalized))
            if found:
                return found

        # Псевдонимы "себя"
        aliases = {"я", "me", "мой", "мне", "себе", "себя",
                   sender.display_name.lower(), sender.first_name.lower()}
        if sender.username:
            aliases.add(sender.username.lower())
            aliases.add(f"@{sender.username.lower()}")
        if normalized in aliases:
            return self.db.get_or_create_user(chat_id, sender)

        # Расширенный поиск с fuzzy + транслитерацией
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
        if any(word in lowered for word in ["люблю", "умница", "хорош", "красава"]):
            delta += 2
        if any(word in lowered for word in ["бесишь", "нахуй", "заебал", "достал"]):
            delta -= 3
        if delta:
            self.moods[chat_id] = max(10, min(100, self.moods[chat_id] + delta))

    def _adjust_mood_from_user_message(self, chat_id: int, user_text: str) -> None:
        lowered = user_text.lower()
        delta = 0
        if any(word in lowered for word in ["спасибо", "обожаю", "люблю", "умница", "красава", "мила"]):
            delta += 5
        if any(word in lowered for word in ["туп", "глуп", "идиот", "нахуй", "пизд", "еба", "дура"]):
            delta -= 8
        self.moods[chat_id] = max(0, min(100, self.moods[chat_id] + delta))

    def _finalize_reply(self, content: str, *, user_text: str) -> str:
        cleaned = content.strip()
        if not cleaned:
            return ""
            
        if self._looks_like_memory_artifact(cleaned):
            return "Чуть не выдала вам базу данных вместо ответа. Давай еще раз."

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

        cleaned = self._enforce_personality_mode(cleaned)
        cleaned = self._soften_personal_attacks(cleaned)
        cleaned = self._trim_incomplete_tail(cleaned)
        if content and not cleaned:
            print(f"⚠️ [AI:finalize] Content was stripped to empty. Original: '{content[:50]}'")
        return cleaned

    def _enforce_personality_mode(self, content: str) -> str:
        # Я убрал удаление нормальных теплых слов, чтобы ИИ мог быть добрым.
        # Оставляем только чистку совсем бредовых "ИИ-галлюцинаций".
        cleaned = content
        ai_hallucinations = ["Я всего лишь языковая модель", "Как искусственный интеллект"]
        for phrase in ai_hallucinations:
            cleaned = re.sub(rf"\b{re.escape(phrase)}\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+([,!.?])", r"\1", cleaned)
        return cleaned.strip(" ,")

    def _soften_personal_attacks(self, content: str) -> str:
        cleaned = content
        cleaned = re.sub(r"\b(?:идиот|дурак|тупой|тупая)\b", "гений мысли", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    def _trim_incomplete_tail(self, content: str) -> str:
        cleaned = content.strip()
        if not cleaned:
            return ""
        dangling_markers = ["«", '"', "“", "„", "(", "["]
        last_sentence_end = max(cleaned.rfind("."), cleaned.rfind("!"), cleaned.rfind("?"))
        if any(cleaned.endswith(marker) for marker in dangling_markers) and last_sentence_end > 10:
            cleaned = cleaned[: last_sentence_end + 1].strip()
        if cleaned.count("«") > cleaned.count("»") and last_sentence_end > 10:
            cleaned = cleaned[: last_sentence_end + 1].strip()
        return cleaned

    def _looks_like_memory_artifact(self, content: str) -> bool:
        normalized = re.sub(r"\s+", " ", content.strip()).casefold()
        artifact_prefixes = ("summary participants:", "summary:", "participants:", "status", "facts:")
        if normalized.startswith(artifact_prefixes):
            return True
        return bool(re.fullmatch(r"(summary|participants|status|facts)(\s+.*)?", normalized))

    def _is_hostile_user_text(self, user_text: str) -> bool:
        lowered = user_text.lower()
        hostile_tokens = ["хуево", "туп", "глуп", "идиот", "дура", "нахуй", "пизд", "еба", "пошла"]
        return any(token in lowered for token in hostile_tokens)

    def _is_media_marker(self, user_text: str) -> bool:
        normalized = user_text.strip().lower()
        if normalized.startswith("[media:"):
            return True
        if "<msg" not in normalized:
            return False
        return not bool(re.search(r'(?i)type="text"', normalized))

    def _extract_current_plain_text(self, user_text: str) -> str:
        # Извлекаем текст из тега <msg> (там сам текст сообщения)
        msg_match = re.search(r"(?is)<msg[^>]*>(.*?)</msg>", user_text)
        if msg_match:
            content = msg_match.group(1).strip()
            # Проверяем, есть ли там только пустые переносы
            if not content:
                # Если текста нет, но это медиа, вернем тип из атрибута type
                type_match = re.search(r'(?i)type="([^"]+)"', user_text)
                if type_match and type_match.group(1) != "text":
                    return f"[{type_match.group(1)}]"
                return ""
            return content
            
        if "<reply_target" in user_text:
            return ""
        return user_text.strip()

    def _extract_current_message_id(self, user_text: str) -> int | None:
        match = re.search(r'(?i)<msg[^>]*id="(\d+)"', user_text)
        if match:
            return int(match.group(1))
        return None

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

    async def _ensure_non_repetitive_reply(self, chat_id: int, messages: list[dict[str, Any]], sender_name: str, user_text: str, content: str) -> str:
        if not self._is_repetitive_reply(chat_id, content):
            return content

        retry = await self._retry_repetitive_reply(messages=messages, sender_name=sender_name, user_text=user_text, previous_reply=content)
        if retry and not self._is_repetitive_reply(chat_id, retry):
            return retry
        return content

    async def _chat_completion(self, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        delay = 0.8
        for attempt in range(1, 4):
            try:
                return await self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt >= 3:
                    break
                self._log("completion_retry", attempt=attempt, error=str(exc)[:180])
                await asyncio.sleep(delay)
                delay *= 2
        assert last_exc is not None
        raise last_exc

    async def _retry_repetitive_reply(self, *, messages: list[dict[str, Any]], sender_name: str, user_text: str, previous_reply: str) -> str:
        retry_messages = [
            *messages,
            {
                "role": "user",
                "content": f"{sender_name}: Дай новый ответ на сообщение '{user_text}', не повторяй формулировку: '{previous_reply}'. Ответ должен быть живым."
            },
        ]
        try:
            response = await self._chat_completion(
                model=self.settings.ai_model, messages=retry_messages, temperature=max(self.settings.ai_temperature, 0.9), max_tokens=min(self.settings.ai_max_tokens, 140)
            )
            raw_text = self._coerce_model_content(response.choices[0].message.content)
            return self._finalize_reply(raw_text, user_text=user_text)
        except Exception:
            return ""

    async def _simple_completion(self, *, model: str, messages: list[dict[str, Any]], temperature: float, max_tokens: int) -> str:
        response = await self._chat_completion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = response.choices[0]
        message = choice.message
        self._log(
            "simple_completion",
            model=model,
            finish_reason=getattr(choice, "finish_reason", None),
            content_type=type(message.content).__name__,
            content_len=len(message.content) if isinstance(message.content, str) else 0,
        )
        return self._coerce_model_content(message.content)

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
                "content": f"{user_name}: Ответь на предыдущее сообщение одной короткой живой репликой. Исходная реплика: {user_text}"
            },
        ]
        try:
            raw_text = await self._simple_completion(
                model=self.settings.ai_model,
                messages=retry_messages,
                temperature=self.settings.ai_temperature,
                max_tokens=min(self.settings.ai_max_tokens, 140),
            )
            finalized = self._finalize_reply(raw_text, user_text=user_text)
            if finalized:
                return finalized
            fallback_model = self.settings.ai_fallback_model
            if fallback_model and fallback_model != self.settings.ai_model:
                raw_text = await self._simple_completion(
                    model=fallback_model,
                    messages=retry_messages,
                    temperature=self.settings.ai_temperature,
                    max_tokens=min(self.settings.ai_max_tokens, 140),
                )
                return self._finalize_reply(raw_text, user_text=user_text)
        except Exception as e:
            self._log("retry_empty_error", error=str(e))
            fallback_model = self.settings.ai_fallback_model
            if fallback_model and fallback_model != self.settings.ai_model:
                try:
                    raw_text = await self._simple_completion(
                        model=fallback_model,
                        messages=retry_messages,
                        temperature=self.settings.ai_temperature,
                        max_tokens=min(self.settings.ai_max_tokens, 140),
                    )
                    return self._finalize_reply(raw_text, user_text=user_text)
                except Exception as fallback_exc:
                    self._log("retry_empty_fallback_error", error=str(fallback_exc))
        return ""
