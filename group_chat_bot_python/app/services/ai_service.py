from __future__ import annotations

from collections import defaultdict, deque

from openai import AsyncOpenAI

from app.config import Settings
from app.models import Sender
from app.services.memory_provider import BaseMemoryProvider
from app.services.persona_service import PersonaService


class AIService:
    def __init__(self, settings: Settings, memory: BaseMemoryProvider, persona: PersonaService) -> None:
        self.settings = settings
        self.memory = memory
        self.persona = persona
        effective_api_key = settings.effective_ai_api_key
        effective_base_url = settings.effective_ai_base_url
        
        self.client = (
            AsyncOpenAI(
                api_key=effective_api_key,
                base_url=effective_base_url,
                timeout=settings.ai_timeout_seconds,
            )
            if effective_api_key
            else None
        )
        self.chat_buffers: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=25))
        self.moods: dict[int, int] = defaultdict(lambda: 50)

    def remember_message(self, chat_id: int, sender: Sender, text: str) -> None:
        self.chat_buffers[chat_id].append(f"{sender.display_name}: {text}")

    async def flush_passive_memory(self, chat_id: int) -> None:
        if len(self.chat_buffers[chat_id]) < 25:
            return
        transcript = "\n".join(self.chat_buffers[chat_id])
        participants: list[str] = []
        for line in self.chat_buffers[chat_id]:
            name = line.split(":", 1)[0]
            if name not in participants:
                participants.append(name)
        await self.memory.save_transcript(chat_id, transcript, participants)
        self.chat_buffers[chat_id].clear()

    async def generate_reply(
        self,
        chat_id: int,
        sender: Sender,
        user_text: str,
        reply_to_bot: bool,
        mentioned: bool,
    ) -> str | None:
        if not self.client or not user_text:
            return None
        if not reply_to_bot and not mentioned:
            return None

        persona_state = self.persona.bump_exchange(chat_id, sender.user_id)
        memory_text = await self.memory.get_relevant_facts(chat_id, user_text, sender.display_name)
        history = list(self.chat_buffers[chat_id])[-10:]
        system_prompt = self._build_system_prompt(persona_state, memory_text, sender.display_name)

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": "user", "content": line} for line in history)
        messages.append({"role": "user", "content": f"{sender.display_name}: {user_text}"})

        response = await self.client.chat.completions.create(
            model=self.settings.ai_model,
            messages=messages,
            temperature=self.settings.ai_temperature,
            max_tokens=self.settings.ai_max_tokens,
        )
        content = (response.choices[0].message.content or "").strip()
        if content:
            self.remember_message(chat_id, Sender(user_id=0, first_name=self.settings.bot_name), content)
            self._adjust_mood(chat_id, content)
        return content or None

    def _build_system_prompt(self, persona_state: dict, memory_text: str, user_name: str) -> str:
        persona_block = (
            f"Ты {self.settings.bot_name}: живая, дерзкая, харизматичная девушка-бот. "
            "Не говори, что ты ИИ или языковая модель. "
            f"Параметры характера: troll={persona_state['troll']}, "
            f"warmth={persona_state['warmth']}, chaos={persona_state['chaos']}."
        )
        memory_block = f"\n\nВот что ты помнишь о чате и пользователях:\n{memory_text}" if memory_text else ""
        return (
            f"{persona_block}{memory_block}\n\n"
            f"Сейчас тебе пишет: {user_name}.\n"
            "Отвечай по-русски, естественно и кратко. "
            "Не выдумывай факты, если их нет в сообщении или памяти. "
            "Если память пустая или сомнительная, опирайся на текущее сообщение."
        )

    def _adjust_mood(self, chat_id: int, reply: str) -> None:
        delta = 0
        lowered = reply.lower()
        if "спасибо" in lowered:
            delta += 2
        if "люблю" in lowered or "обожаю" in lowered:
            delta += 1
        self.moods[chat_id] = max(0, min(100, self.moods[chat_id] + delta))
