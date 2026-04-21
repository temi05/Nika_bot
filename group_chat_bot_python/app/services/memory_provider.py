from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.models import MemoryRecord
from app.services.prompt_builders import build_memory_extraction_messages, format_memory_context
from app.services.supabase_db import SupabaseDB


def _compact_transcript(transcript: str, limit: int = 3000) -> str:
    return "\n".join(line.strip() for line in transcript.splitlines() if line.strip())[:limit]


def _parse_json_content(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def _looks_memory_worthy_message(message: str) -> bool:
    normalized = message.strip()
    lowered = normalized.casefold()
    if len(normalized) < 12:
        return False
    if any(token in lowered for token in ["ахах", "пхах", "лол", "кек", "хаха", ")))", "))))", "ыыы"]):
        return False
    if re.fullmatch(r"[\W\d_]+", normalized):
        return False
    return True


def _is_memory_artifact(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", (text or "").strip()).casefold()
    if not normalized:
        return True
    artifact_markers = [
        "summary participants:",
        "participants:",
        "summary:",
        "status",
        "low-confidence fallback memory",
    ]
    if any(normalized.startswith(marker) for marker in artifact_markers):
        return True
    return normalized in {"summary", "participants", "status", "facts"}


def _clean_memory_items(items: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        value = (item or "").strip()
        if not value or _is_memory_artifact(value):
            continue
        cleaned.append(value)
    return cleaned


class BaseMemoryProvider:
    async def save_transcript(self, chat_id: int, transcript: str, participants: list[str]) -> None:
        raise NotImplementedError

    async def get_relevant_facts(self, chat_id: int, user_message: str, user_name: str) -> str:
        raise NotImplementedError

    async def health(self) -> dict:
        return {"healthy": True}


class DatabaseMemoryProvider(BaseMemoryProvider):
    def __init__(self, settings: Settings, db: SupabaseDB) -> None:
        self.settings = settings
        self.db = db
        self.client = (
            AsyncOpenAI(
                api_key=settings.effective_ai_api_key,
                base_url=settings.effective_ai_base_url,
                timeout=settings.ai_timeout_seconds,
            )
            if settings.memory_extraction_enabled and settings.effective_ai_api_key
            else None
        )

    def _log(self, event: str, **kwargs: Any) -> None:
        details = " ".join(f"{key}={value!r}" for key, value in kwargs.items())
        print(f"[MEMORY:{event}] {details}".strip())

    async def save_transcript(self, chat_id: int, transcript: str, participants: list[str]) -> None:
        compact = _compact_transcript(transcript)
        if not compact:
            return

        extracted = await self._extract_memories(compact, participants)
        if not extracted:
            # fallback: выбираем достаточно длинные и осмысленные строки
            extracted = self._build_fallback_extracted(compact, participants)

        stored = 0

        summary = extracted.get("summary", "").strip()
        if summary and not self.db.memory_exists(chat_id, summary):
            self.db.store_memory(
                chat_id,
                MemoryRecord(
                    fact=summary,
                    source="conversation_summary",
                    confidence=0.6,
                    meta={"participants": participants},
                ),
            )
            stored += 1

        for item in extracted.get("facts", [])[: self.settings.memory_extraction_max_facts]:
            fact = str(item.get("fact") or "").strip()
            if not fact:
                continue

            confidence = float(item.get("confidence") or 0.0)
            if confidence < self.settings.memory_fact_min_confidence:
                continue
            if self.db.memory_exists(chat_id, fact):
                continue

            self.db.store_memory(
                chat_id,
                MemoryRecord(
                    fact=fact,
                    source=str(item.get("source") or "memory_fact")[:40],
                    confidence=confidence,
                    meta={
                        "participants": participants,
                        "entities": item.get("entities") or [],
                        "tags": item.get("tags") or [],
                    },
                ),
            )
            stored += 1

        if stored == 0:
            self._log("skip_store", chat_id=chat_id, participants=participants, reason="no_clean_memories")
        else:
            self._log("stored", chat_id=chat_id, count=stored, participants=participants)

    async def get_relevant_facts(self, chat_id: int, user_message: str, user_name: str) -> str:
        tokens = list(dict.fromkeys(re.findall(r"[\w@-]{4,}", user_message.lower())))[:6]
        profile_facts: list[str] = []
        topic_facts: list[str] = []

        if user_name:
            profile_facts.extend(self.db.get_all_user_facts(chat_id, user_name, limit=4))

        for token in tokens:
            topic_facts.extend(self.db.search_memory(chat_id, token, limit=3))

        recent_facts = self.db.get_recent_memories(chat_id, limit=4)
        context = format_memory_context(
            profile_facts=_clean_memory_items(list(dict.fromkeys(profile_facts)))[:4],
            topic_facts=_clean_memory_items(list(dict.fromkeys(topic_facts)))[: self.settings.memory_retrieval_limit],
            recent_facts=_clean_memory_items(list(dict.fromkeys(recent_facts)))[:4],
        )
        self._log("retrieve", chat_id=chat_id, tokens=tokens, has_context=bool(context))
        return context

    async def _extract_memories(self, transcript: str, participants: list[str]) -> dict[str, Any]:
        if not self.client:
            self._log("extract_skipped", reason="no_client")
            return {}

        try:
            messages = build_memory_extraction_messages(
                bot_name=self.settings.bot_name,
                transcript=transcript,
                participants=participants,
            )
            response = await self.client.chat.completions.create(
                model=self.settings.effective_memory_model,
                messages=messages,
                temperature=0.2,
                max_tokens=900,
            )
            content = (response.choices[0].message.content or "").strip()
            payload = _parse_json_content(content)
            if isinstance(payload, dict):
                self._log("extract_ok", facts=len(payload.get("facts") or []))
                return payload
        except Exception as exc:
            self._log("extract_error", error=str(exc))
        return {}

    def _build_fallback_extracted(self, transcript: str, participants: list[str]) -> dict[str, Any]:
        """Простой fallback без LLM: берём осмысленные пользовательские строки."""
        bot_markers = {self.settings.bot_name.casefold(), f"@{self.settings.bot_name.casefold()}"}
        facts: list[dict[str, Any]] = []

        for raw_line in transcript.splitlines():
            line = raw_line.strip()
            if not line or ": " not in line:
                continue
            speaker, message = line.split(": ", 1)
            message = re.sub(r"^\[media:[^\]]+\]\s*", "", message, flags=re.IGNORECASE).strip()
            if not message or speaker.casefold() in bot_markers:
                continue
            if not _looks_memory_worthy_message(message):
                continue
            facts.append(
                {
                    "fact": f"{speaker}: {message[:180]}",
                    "source": "fallback_user_signal",
                    "confidence": 0.72,
                    "entities": [speaker],
                    "tags": ["fallback"],
                }
            )
            if len(facts) >= 6:
                break

        if not facts:
            return {}
        return {"summary": "", "facts": facts, "participants": participants}


def build_memory_provider(settings: Settings, db: SupabaseDB) -> BaseMemoryProvider:
    return DatabaseMemoryProvider(settings, db)
