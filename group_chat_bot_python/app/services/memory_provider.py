from __future__ import annotations

import json
import re
from typing import Any

import httpx
from openai import AsyncOpenAI

from app.config import Settings
from app.models import MemoryRecord
from app.services.prompt_builders import build_memory_extraction_messages, format_memory_context
from app.services.supabase_db import SupabaseDB


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
        compact = "\n".join(line.strip() for line in transcript.splitlines() if line.strip())[:3000]
        if not compact:
            return

        extracted = await self._extract_memories(compact, participants)
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
            fallback = compact.replace("\n", " ")[:1200]
            if fallback and not self.db.memory_exists(chat_id, fallback):
                self.db.store_memory(chat_id, MemoryRecord(fact=fallback, source="transcript_fallback", confidence=0.4))
            self._log("fallback_store", chat_id=chat_id, participants=participants)
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
            profile_facts=list(dict.fromkeys(profile_facts))[:4],
            topic_facts=list(dict.fromkeys(topic_facts))[: self.settings.memory_retrieval_limit],
            recent_facts=list(dict.fromkeys(recent_facts))[:4],
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
            payload = self._parse_json_content(content)
            if isinstance(payload, dict):
                self._log("extract_ok", facts=len(payload.get("facts") or []))
                return payload
        except Exception as exc:
            self._log("extract_error", error=str(exc))
        return {}

    def _parse_json_content(self, content: str) -> dict[str, Any]:
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


class LightRAGMemoryProvider(BaseMemoryProvider):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.lightrag_base_url.rstrip("/")

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self.settings.lightrag_api_key:
            headers["X-API-Key"] = self.settings.lightrag_api_key
        async with httpx.AsyncClient(timeout=self.settings.lightrag_timeout_seconds) as client:
            response = await client.request(method, f"{self.base_url}{path}", json=json, headers=headers)
            response.raise_for_status()
            return response.json()

    async def save_transcript(self, chat_id: int, transcript: str, participants: list[str]) -> None:
        payload = {"text": transcript}
        if self.settings.lightrag_workspace:
            payload["workspace"] = self.settings.lightrag_workspace
        await self._request("POST", "/documents/text", payload)

    async def get_relevant_facts(self, chat_id: int, user_message: str, user_name: str) -> str:
        payload = {
            "query": f"{user_name}: {user_message}",
            "mode": self.settings.lightrag_query_mode,
            "only_need_context": True,
            "include_references": True,
            "include_chunk_content": True,
        }
        if self.settings.lightrag_workspace:
            payload["workspace"] = self.settings.lightrag_workspace

        try:
            data = await self._request("POST", "/query", payload)
            if isinstance(data.get("context"), str):
                return data["context"]
            if isinstance(data.get("response"), str):
                return data["response"]
        except Exception as exc:
            print(f"[MEMORY:lightrag_error] error={exc}")
        return ""

    async def health(self) -> dict:
        try:
            data = await self._request("GET", "/health")
            return {"healthy": True, "upstream": data}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}


def build_memory_provider(settings: Settings, db: SupabaseDB) -> BaseMemoryProvider:
    if settings.memory_provider.lower() == "lightrag":
        return LightRAGMemoryProvider(settings)
    return DatabaseMemoryProvider(settings, db)
