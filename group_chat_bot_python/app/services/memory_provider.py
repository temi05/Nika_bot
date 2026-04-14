from __future__ import annotations

import re

import httpx

from app.config import Settings
from app.models import MemoryRecord
from app.services.supabase_db import SupabaseDB


class BaseMemoryProvider:
    async def save_transcript(self, chat_id: int, transcript: str, participants: list[str]) -> None:
        raise NotImplementedError

    async def get_relevant_facts(self, chat_id: int, user_message: str, user_name: str) -> str:
        raise NotImplementedError

    async def health(self) -> dict:
        return {"healthy": True}


class DatabaseMemoryProvider(BaseMemoryProvider):
    def __init__(self, db: SupabaseDB) -> None:
        self.db = db

    async def save_transcript(self, chat_id: int, transcript: str, participants: list[str]) -> None:
        compact = " ".join(line.strip() for line in transcript.splitlines() if line.strip())[:1200]
        if compact:
            self.db.store_memory(chat_id, MemoryRecord(fact=compact, source="transcript"))

    async def get_relevant_facts(self, chat_id: int, user_message: str, user_name: str) -> str:
        tokens = re.findall(r"[\w@-]{4,}", user_message.lower())[:5]
        lines: list[str] = []
        if user_name:
            lines.extend(self.db.get_all_user_facts(chat_id, user_name, limit=4))
        for token in tokens:
            lines.extend(self.db.search_memory(chat_id, token, limit=2))
        deduped = list(dict.fromkeys(line for line in lines if line))
        return "\n".join(f"- {line}" for line in deduped[:6])


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
        payload = {"text": f"[chat_id:{chat_id}]\nParticipants: {', '.join(participants)}\nTranscript:\n{transcript}"}
        if self.settings.lightrag_workspace:
            payload["workspace"] = self.settings.lightrag_workspace
        await self._request("POST", "/documents/text", payload)

    async def get_relevant_facts(self, chat_id: int, user_message: str, user_name: str) -> str:
        payload = {
            "query": f"Chat ID: {chat_id}\nCurrent speaker: {user_name}\nQuestion: {user_message}",
            "mode": self.settings.lightrag_query_mode,
            "only_need_context": True,
            "include_references": True,
            "include_chunk_content": True,
        }
        if self.settings.lightrag_workspace:
            payload["workspace"] = self.settings.lightrag_workspace

        data = await self._request("POST", "/query", payload)
        if isinstance(data.get("context"), str):
            return data["context"]
        if isinstance(data.get("response"), str):
            return data["response"]
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
    return DatabaseMemoryProvider(db)
