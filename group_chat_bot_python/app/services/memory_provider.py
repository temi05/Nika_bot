from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from openai import AsyncOpenAI

from app.config import Settings
from app.models import MemoryRecord, MemorySyncJob
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


def _render_memory_document(extracted: dict[str, Any], participants: list[str]) -> str:
    if not isinstance(extracted, dict):
        return ""

    summary = str(extracted.get("summary") or "").strip()
    facts = [item for item in (extracted.get("facts") or []) if isinstance(item, dict)]
    fact_lines: list[str] = []
    for item in facts[:8]:
        fact = str(item.get("fact") or "").strip()
        if not fact:
            continue
        source = str(item.get("source") or "memory").strip() or "memory"
        confidence = item.get("confidence")
        entities = [str(entity).strip() for entity in (item.get("entities") or []) if str(entity).strip()]
        tags = [str(tag).strip() for tag in (item.get("tags") or []) if str(tag).strip()]

        line = f"- [{source}] {fact}"
        if confidence is not None:
            line += f" (confidence={confidence})"
        if entities:
            line += f" | entities: {', '.join(entities[:5])}"
        if tags:
            line += f" | tags: {', '.join(tags[:5])}"
        fact_lines.append(line)

    sections: list[str] = []
    if participants:
        sections.append("Participants: " + ", ".join(participants))
    if summary:
        sections.append("Summary:\n" + summary)
    if fact_lines:
        sections.append("Facts:\n" + "\n".join(fact_lines))
    return "\n\n".join(section for section in sections if section).strip()


def _build_filtered_fallback_document(transcript: str, participants: list[str], bot_name: str) -> str:
    highlights: list[str] = []
    bot_markers = {bot_name.casefold(), f"@{bot_name.casefold()}"}

    for raw_line in transcript.splitlines():
        line = raw_line.strip()
        if not line or ": " not in line:
            continue
        speaker, message = line.split(": ", 1)
        if not message or message == "[media]":
            continue
        if speaker.casefold() in bot_markers:
            continue
        if not _looks_memory_worthy_message(message):
            continue
        highlights.append(f"- [user_signal] {speaker}: {message[:180]}")
        if len(highlights) >= 6:
            break

    if not highlights:
        return ""

    sections = []
    if participants:
        sections.append("Participants: " + ", ".join(participants))
    sections.append("Summary:\nLow-confidence fallback memory built from user-only highlights.")
    sections.append("Facts:\n" + "\n".join(highlights))
    return "\n\n".join(sections)


def _looks_memory_worthy_message(message: str) -> bool:
    normalized = message.strip()
    lowered = normalized.casefold()
    if len(normalized) < 12:
        return False
    if any(token in lowered for token in ["ахах", "пхах", "лол", "кек", "хаха", ")))", ")))", "ыыы"]):
        return False
    if re.fullmatch(r"[\W\d_]+", normalized):
        return False
    return True


async def _extract_memories_with_client(
    client: AsyncOpenAI | None,
    settings: Settings,
    transcript: str,
    participants: list[str],
    log_fn,
) -> dict[str, Any]:
    if not client:
        log_fn("extract_skipped", reason="no_client")
        return {}

    try:
        messages = build_memory_extraction_messages(
            bot_name=settings.bot_name,
            transcript=transcript,
            participants=participants,
        )
        response = await client.chat.completions.create(
            model=settings.effective_memory_model,
            messages=messages,
            temperature=0.2,
            max_tokens=900,
        )
        content = (response.choices[0].message.content or "").strip()
        payload = _parse_json_content(content)
        if isinstance(payload, dict):
            log_fn("extract_ok", facts=len(payload.get("facts") or []))
            return payload
    except Exception as exc:
        log_fn("extract_error", error=str(exc))
    return {}


class BaseMemoryProvider:
    async def save_transcript(self, chat_id: int, transcript: str, participants: list[str]) -> None:
        raise NotImplementedError

    async def get_relevant_facts(self, chat_id: int, user_message: str, user_name: str) -> str:
        raise NotImplementedError

    async def flush_pending_queue(self) -> None:
        return None

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
            profile_facts=list(dict.fromkeys(profile_facts))[:4],
            topic_facts=list(dict.fromkeys(topic_facts))[: self.settings.memory_retrieval_limit],
            recent_facts=list(dict.fromkeys(recent_facts))[:4],
        )
        self._log("retrieve", chat_id=chat_id, tokens=tokens, has_context=bool(context))
        return context

    async def _extract_memories(self, transcript: str, participants: list[str]) -> dict[str, Any]:
        return await _extract_memories_with_client(self.client, self.settings, transcript, participants, self._log)


class LightRAGMemoryProvider(BaseMemoryProvider):
    def __init__(self, settings: Settings, db: SupabaseDB) -> None:
        self.settings = settings
        self.db = db
        self.base_url = settings.lightrag_base_url.rstrip("/")
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

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self.settings.lightrag_api_key:
            headers["X-API-Key"] = self.settings.lightrag_api_key
        async with httpx.AsyncClient(timeout=self.settings.lightrag_timeout_seconds) as client:
            response = await client.request(method, f"{self.base_url}{path}", json=json, headers=headers)
            response.raise_for_status()
            return response.json()

    async def save_transcript(self, chat_id: int, transcript: str, participants: list[str]) -> None:
        compact = _compact_transcript(transcript)
        if not compact:
            return
        document = await self._build_memory_document(compact, participants)
        if not document:
            self._log("skip_queue", chat_id=chat_id, participants=participants, reason="no_clean_document")
            return

        job = self.db.enqueue_memory_sync(
            chat_id=chat_id,
            transcript=document,
            participants=participants,
            provider="lightrag",
            workspace=self.settings.lightrag_workspace,
        )
        if not job:
            print(f"[MEMORY:lightrag_queue_error] chat_id={chat_id} error='enqueue_failed'")
            return

        try:
            await self._deliver_sync_job(job)
            self.db.mark_memory_sync_done(job.id)
            print(f"[MEMORY:lightrag_sync_done] job_id={job.id} chat_id={chat_id} mode='inline'")
        except Exception as exc:
            self._schedule_retry(job, exc)

    async def flush_pending_queue(self) -> None:
        jobs = self.db.get_due_memory_sync_jobs("lightrag", limit=self.settings.memory_sync_batch_size)
        if not jobs:
            return

        for job in jobs:
            try:
                await self._deliver_sync_job(job)
                self.db.mark_memory_sync_done(job.id)
                print(f"[MEMORY:lightrag_sync_done] job_id={job.id} chat_id={job.chat_id} mode='worker'")
            except Exception as exc:
                self._schedule_retry(job, exc)

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
        stats = self.db.get_memory_sync_stats("lightrag")
        try:
            data = await self._request("GET", "/health")
            return {"healthy": True, "upstream": data, "queue": stats}
        except Exception as exc:
            return {"healthy": False, "error": str(exc), "queue": stats}

    async def _deliver_sync_job(self, job: MemorySyncJob) -> None:
        payload = {"text": job.transcript}
        if job.workspace:
            payload["workspace"] = job.workspace
        await self._request("POST", "/documents/text", payload)

    async def _build_memory_document(self, transcript: str, participants: list[str]) -> str:
        extracted = await _extract_memories_with_client(self.client, self.settings, transcript, participants, self._log)
        document = _render_memory_document(extracted, participants)
        if document:
            return document
        return _build_filtered_fallback_document(transcript, participants, self.settings.bot_name)

    def _schedule_retry(self, job: MemorySyncJob, exc: Exception) -> None:
        attempts = job.attempts + 1
        error_text = str(exc)
        if attempts >= self.settings.memory_sync_max_attempts:
            self.db.mark_memory_sync_failed(job.id, attempts, error_text)
            print(
                "[MEMORY:lightrag_sync_failed] "
                f"job_id={job.id} chat_id={job.chat_id} attempts={attempts} error={error_text}"
            )
            return

        delay = min(
            self.settings.memory_sync_retry_max_seconds,
            self.settings.memory_sync_retry_base_seconds * (2 ** max(attempts - 1, 0)),
        )
        next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        self.db.reschedule_memory_sync_job(job.id, attempts, next_attempt_at, error_text)
        print(
            "[MEMORY:lightrag_sync_retry] "
            f"job_id={job.id} chat_id={job.chat_id} attempts={attempts} "
            f"retry_in={delay}s error={error_text}"
        )


def build_memory_provider(settings: Settings, db: SupabaseDB) -> BaseMemoryProvider:
    if settings.memory_provider.lower() == "lightrag":
        return LightRAGMemoryProvider(settings, db)
    return DatabaseMemoryProvider(settings, db)
