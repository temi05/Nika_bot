from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.models import MemoryRecord
from app.services.memory_provider import BaseMemoryProvider, _compact_transcript, _clean_memory_items, _parse_json_content
from app.services.prompt_builders import build_memory_extraction_messages, format_memory_context
from app.services.supabase_db import SupabaseDB
from app.services.telegram_backup import TelegramBackupService


def _md5_short(text: str, length: int = 12) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:length]


def _normalize_fact_text(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").casefold()).strip()


def _resolve_entity_name(fact: str, item_entities: Any, participants: list[str]) -> str:
    if isinstance(item_entities, list):
        for ent in item_entities:
            clean_ent = str(ent or "").strip()
            if clean_ent:
                return clean_ent
    fact_lower = (fact or "").casefold()
    for participant in participants:
        clean_part = str(participant or "").strip()
        bare_part = clean_part.lstrip("@").casefold()
        if bare_part and len(bare_part) >= 2 and bare_part in fact_lower:
            return clean_part
    return ""


def _iter_docs_newest_first(
    docs: list[str],
    metas: list[dict[str, Any] | None],
) -> list[tuple[str, dict[str, Any]]]:
    indexed: list[tuple[str, int, str, dict[str, Any]]] = []
    for idx, (doc, meta) in enumerate(zip(docs, metas)):
        safe_meta = dict(meta or {})
        updated_at = str(safe_meta.get("updated_at") or "")
        indexed.append((updated_at, idx, doc, safe_meta))
    indexed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(doc, safe_meta) for _, _, doc, safe_meta in indexed]


class LightweightEmbeddingFunction:
    """Легковесный математический векторный эмбеддер для экономии RAM на Render (<20 MB)"""
    def __init__(self, dim: int = 128) -> None:
        self.dim = dim

    def name(self) -> str:
        return "lightweight_embedding_function"

    def is_legacy(self) -> bool:
        return True

    def embed_documents(self, input: list[str]) -> list[list[float]]:
        embeddings = []
        for text in input:
            tokens = re.findall(r"\w+", text.lower())
            vec = [0.0] * self.dim
            for token in tokens:
                idx = int(hashlib.md5(token.encode("utf-8")).hexdigest()[:8], 16) % self.dim
                vec[idx] += 1.0
            norm = sum(x * x for x in vec) ** 0.5
            if norm > 0:
                vec = [x / norm for x in vec]
            embeddings.append(vec)
        return embeddings

    def embed_query(self, input: Any) -> Any:
        if isinstance(input, str):
            return self.embed_documents([input])[0]
        elif isinstance(input, list):
            return self.embed_documents(input)
        return self.embed_documents([str(input)])

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self.embed_documents(input)


def _clean_legacy_fact(text: str) -> str:
    if not text:
        return ""
    cleaned = text.strip()

    ephemeral = [
        "заснул", "хочет спать", "хочет пасочку", "покрасить яички",
        "пошел кушать", "отрубилась", "легла", "потянувся", "проспала",
        "недопонимание", "эмоция:", "состояние:", "состояние ",
        "ем пельмени", "ем чипсы", "выпил чаю", "застрял в пробке",
        "играет прямо сейчас", "сидит в телефоне", "пошел спать", "устал"
    ]
    if any(k in cleaned.lower() for k in ephemeral):
        return ""

    cleaned = re.sub(r"^УЗЕЛ:\s*", "", cleaned)
    cleaned = re.sub(r"^СВЯЗЬ:\s*", "", cleaned)
    cleaned = re.sub(r"\s*\|\s*интерес:\s*", " увлекается: ", cleaned)
    cleaned = re.sub(r"\s*\|\s*предпочитает:\s*", " предпочитает ", cleaned)
    cleaned = re.sub(r"\s*\|\s*факт:\s*", " — ", cleaned)
    cleaned = re.sub(r"\s*\|\s*роль:\s*", " имеет роль ", cleaned)
    cleaned = re.sub(r"\s*\|\s*прозвище:\s*", " имеет прозвище ", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if len(cleaned) >= 5 else ""


class ChromaMemoryProvider(BaseMemoryProvider):
    def __init__(
        self,
        settings: Settings,
        db: SupabaseDB,
        backup_service: TelegramBackupService | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.db._chroma_memory = self  # type: ignore[attr-defined]
        self.backup_service = backup_service
        self.data_dir = Path(__file__).resolve().parents[2] / "data" / "chroma_db"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.client = (
            AsyncOpenAI(
                api_key=settings.effective_ai_api_key,
                base_url=settings.effective_ai_base_url,
                timeout=settings.ai_timeout_seconds,
            )
            if settings.effective_ai_api_key
            else None
        )

        self._chroma_client = None
        self._collection = None
        self._embedding_function = LightweightEmbeddingFunction()
        self._migrated_from_supabase = False
        self._init_chroma()

    def _log(self, action: str, **kwargs: Any) -> None:
        details = " ".join(f"{key}={value!r}" for key, value in kwargs.items())
        print(f"[CHROMA_MEMORY:{action}] {details}".strip())

    def _sync_upsert_db(
        self,
        chat_id: int,
        doc_id: str,
        fact: str,
        *,
        entity_name: str = "",
        source: str = "fact",
        confidence: float = 0.8,
        source_message_id: int | None = None,
        user_id: int | None = None,
    ) -> None:
        if not hasattr(self.db, "upsert_chroma_fact_to_knowledge"):
            return
        try:
            self.db.upsert_chroma_fact_to_knowledge(
                chat_id,
                doc_id,
                fact,
                entity_name=entity_name,
                source=source,
                confidence=confidence,
                source_message_id=source_message_id,
                user_id=user_id,
            )
        except Exception as e:
            self._log("db_sync_upsert_error", error=str(e))

    def _sync_delete_db(self, chat_id: int, doc_ids: list[str] | str) -> None:
        if not hasattr(self.db, "delete_knowledge_by_chroma_id"):
            return
        try:
            self.db.delete_knowledge_by_chroma_id(chat_id, doc_ids)
        except Exception as e:
            self._log("db_sync_delete_error", error=str(e))

    def _sync_update_db(self, chat_id: int, doc_id: str, new_text: str) -> None:
        if not hasattr(self.db, "update_knowledge_by_chroma_id"):
            return
        try:
            self.db.update_knowledge_by_chroma_id(chat_id, doc_id, new_text)
        except Exception as e:
            self._log("db_sync_update_error", error=str(e))

    def _init_chroma(self) -> None:
        try:
            import chromadb

            self._chroma_client = chromadb.PersistentClient(path=str(self.data_dir))
            self._collection = self._chroma_client.get_or_create_collection(
                name="nika_vector_memory",
                embedding_function=self._embedding_function,
                metadata={"hnsw:space": "cosine"},
            )
            self._log("init_success", count=self._collection.count())
            self._migrate_if_needed()
        except Exception as e:
            self._log("init_error", error=str(e))

    async def restore_from_zip_bytes(self, zip_bytes: bytes) -> bool:
        """Восстанавливает ChromaDB напрямую из загруженных байтов ZIP архива"""
        if not self.backup_service:
            return False
        success = await self.backup_service.restore_from_zip_bytes(zip_bytes)
        if success:
            self._init_chroma()
        return success

    async def health(self) -> dict[str, Any]:
        if self._collection is None:
            return {"healthy": False, "provider": "chroma", "count": 0}
        try:
            count = self._collection.count()
            return {"healthy": True, "provider": "chroma", "count": count}
        except Exception as e:
            return {"healthy": False, "provider": "chroma", "error": str(e)}

    def _migrate_if_needed(self) -> None:
        """Восстановление/перенос фактов из Supabase bot_knowledge в ChromaDB при холодном старте"""
        if not self._collection or self._collection.count() > 0 or self._migrated_from_supabase:
            return

        try:
            self._log("migration_start", status="fetching_from_supabase")
            records = self.db._knowledge().select("*").execute()
            if not records or not records.data:
                self._log("migration_empty", reason="no_records_in_supabase")
                self._migrated_from_supabase = True
                return

            by_id: dict[str, tuple[str, dict[str, Any]]] = {}

            for item in records.data:
                rec_id = item.get("id")
                raw_fact = (item.get("fact") or "").strip()
                fact_text = _clean_legacy_fact(raw_fact)
                if not fact_text:
                    if rec_id:
                        try:
                            self.db._knowledge().delete().eq("id", rec_id).execute()
                        except Exception:
                            pass
                    continue

                if fact_text != raw_fact and rec_id:
                    try:
                        self.db._knowledge().update({"fact": fact_text}).eq("id", rec_id).execute()
                    except Exception:
                        pass

                raw_meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
                chat_id = str(item.get("chat_id") or "0")
                entity_name = str(item.get("entity_name") or raw_meta.get("entity_name") or "")
                source = str(item.get("fact_type") or raw_meta.get("source") or "fact")
                confidence = float(item.get("confidence") or 0.55)
                updated_at = str(item.get("last_seen_at") or item.get("created_at") or "")

                stored_cid = str(raw_meta.get("chroma_id") or "").strip()
                if stored_cid:
                    doc_id = stored_cid
                else:
                    if source in {"event_timeline", "event"}:
                        prefix = "event"
                    elif source == "manual_admin":
                        prefix = "manual"
                    else:
                        prefix = "fact"
                    doc_id = f"{prefix}_{chat_id}_{_md5_short(fact_text)}"

                meta_dict: dict[str, Any] = {
                    "chat_id": chat_id,
                    "entity_name": entity_name,
                    "source": source,
                    "confidence": confidence,
                    "updated_at": updated_at,
                }
                src_mid = item.get("source_message_id") or raw_meta.get("source_message_id")
                if src_mid is not None:
                    try:
                        meta_dict["source_message_id"] = int(src_mid)
                    except (TypeError, ValueError):
                        pass
                uid_val = item.get("entity_user_id") or raw_meta.get("user_id") or raw_meta.get("entity_user_id")
                if uid_val is not None:
                    try:
                        meta_dict["user_id"] = int(uid_val)
                    except (TypeError, ValueError):
                        pass

                by_id[doc_id] = (fact_text, meta_dict)

            if by_id:
                ids = list(by_id.keys())
                documents = [by_id[cid][0] for cid in ids]
                metadatas = [by_id[cid][1] for cid in ids]
                self._collection.upsert(documents=documents, metadatas=metadatas, ids=ids)
                self._log("migration_success", imported_count=len(documents))
            self._migrated_from_supabase = True
        except Exception as e:
            self._log("migration_error", error=str(e))

    def _profile_facts_from_db(self, chat_id: int, user_id: int | None) -> list[str]:
        if not user_id or user_id <= 0:
            return []
        try:
            user = self.db.get_user_by_platform_id(chat_id, user_id)
        except Exception:
            user = None
        if not user:
            return []
        facts: list[str] = []
        name = user.display_name
        if user.bio:
            facts.append(f"{name} рассказал о себе: {user.bio}")
        if user.ai_notes:
            for raw_line in user.ai_notes.splitlines():
                note = raw_line.strip(" -\t")
                if note:
                    facts.append(f"{name}: {note}")
        if user.birthday:
            facts.append(f"{name} день рождения: {user.birthday}")
        if user.flavor:
            facts.append(f"{name} стиль общения/вайб: {user.flavor}")
        return facts[:8]

    async def get_relevant_facts(
        self,
        chat_id: int,
        user_message: str,
        user_name: str,
        user_id: int | None = None,
    ) -> str:
        if not self._collection:
            return ""

        try:
            db_profile_facts: list[str] = self._profile_facts_from_db(chat_id, user_id)
            chroma_user_facts: list[str] = []
            mentioned_facts: list[str] = []

            all_data = self._collection.get(where={"chat_id": str(chat_id)})
            event_facts: list[str] = []
            if all_data and all_data.get("documents"):
                ordered_pairs = _iter_docs_newest_first(
                    all_data["documents"],
                    all_data.get("metadatas") or [],
                )
                clean_uname = (user_name or "").strip().casefold()

                # 1. Свежие факты об отправителе и важные события чата (сначала самые новые!)
                for doc, meta in ordered_pairs:
                    src = str(meta.get("source") or "")
                    if src in {"event_timeline", "event"} or "событие" in src.lower():
                        event_facts.append(doc)
                    elif (
                        (user_id and meta.get("user_id") == user_id)
                        or (clean_uname and str(meta.get("entity_name") or "").strip().casefold() == clean_uname)
                        or (clean_uname and clean_uname in doc.casefold())
                    ):
                        chroma_user_facts.append(doc)

                # 2. Точный поиск фактов об упомянутых участниках/сущностях в сообщении
                raw_words = [w.strip(" .,!?:;\"'()[]{}<>-").casefold() for w in re.findall(r"\b[\w\d_@]+\b", user_message)]
                stop_words = {"расскажи", "что", "про", "тебя", "меня", "кого", "какое", "знаешь", "есть", "тогда", "скажи", "какой", "какая", "почему", "зачем", "когда", "где", "как", "еще", "ещё"}
                target_words = [w for w in raw_words if len(w) >= 3 and w not in stop_words]

                for doc, meta in ordered_pairs:
                    doc_lower = doc.casefold()
                    entity_lower = str(meta.get("entity_name") or "").casefold()
                    for word in target_words:
                        stem = word[:4] if len(word) >= 5 else word
                        if stem in doc_lower or (entity_lower and stem in entity_lower):
                            mentioned_facts.append(doc)
                            break

            # 3. Векторный семантический поиск по смыслу сообщения
            semantic_facts: list[str] = []
            if user_message and len(user_message.strip()) >= 3:
                results = self._collection.query(
                    query_texts=[user_message],
                    n_results=min(self.settings.memory_retrieval_limit, 8),
                    where={"chat_id": str(chat_id)},
                )
                if results and results.get("documents") and results["documents"][0]:
                    semantic_facts = results["documents"][0]

            meme_facts: list[str] = []
            try:
                meme_facts = self.db.search_meme_knowledge(chat_id, user_message, limit=4)
            except Exception:
                pass

            profile_combined = _clean_memory_items(
                list(dict.fromkeys(chroma_user_facts[:5] + mentioned_facts[:4] + db_profile_facts[:3]))
            )[:6]
            topic_combined = _clean_memory_items(
                list(dict.fromkeys(semantic_facts[:5] + meme_facts[:4]))
            )[:6]
            events_combined = _clean_memory_items(list(dict.fromkeys(event_facts[:5])))[:5]

            return format_memory_context(
                profile_facts=profile_combined,
                topic_facts=topic_combined,
                recent_facts=events_combined,
            )
        except Exception as e:
            self._log("get_facts_error", error=str(e))
            return ""

    def add_chat_event(self, chat_id: int, event_text: str, entity_name: str = "") -> bool:
        """Сохраняет ключевое событие из жизни чата (дуэль, казино, спор, важный факт)"""
        if not self._collection or not event_text.strip():
            return False
        try:
            clean_event = event_text.strip()
            doc_id = f"event_{chat_id}_{_md5_short(clean_event)}"
            now_iso = datetime.now(timezone.utc).isoformat()
            self._collection.upsert(
                documents=[clean_event],
                metadatas=[{
                    "chat_id": str(chat_id),
                    "entity_name": entity_name,
                    "source": "event_timeline",
                    "confidence": 0.95,
                    "updated_at": now_iso,
                }],
                ids=[doc_id],
            )
            self._sync_upsert_db(
                chat_id,
                doc_id,
                clean_event,
                entity_name=entity_name,
                source="event_timeline",
                confidence=0.95,
            )
            self._log("chat_event_stored", chat_id=chat_id, event=clean_event[:60])
            if self.backup_service:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.backup_service.upload_backup("💾 Авто-бэкап (новое событие чата)"))
                except RuntimeError:
                    pass
            return True
        except Exception as e:
            self._log("add_chat_event_error", error=str(e))
            return False

    async def save_transcript(self, chat_id: int, transcript: str, participants: list[str]) -> None:
        compact = _compact_transcript(transcript)
        if not compact or not self._collection:
            return

        extracted = await self._extract_memories(compact, participants)
        if not extracted:
            return

        existing_items: list[tuple[str, str, str]] = []
        try:
            existing_data = self._collection.get(where={"chat_id": str(chat_id)})
            if existing_data and existing_data.get("ids"):
                for ex_id, ex_doc in zip(existing_data["ids"], existing_data.get("documents") or []):
                    ex_str = str(ex_doc or "").strip()
                    if ex_str:
                        existing_items.append((ex_id, ex_str, _normalize_fact_text(ex_str)))
        except Exception:
            pass

        stored = 0
        for item in extracted.get("facts", [])[: self.settings.memory_extraction_max_facts]:
            fact = str(item.get("fact") or "").strip()
            if not fact:
                continue

            confidence = float(item.get("confidence") or 0.0)
            if confidence < self.settings.memory_fact_min_confidence:
                continue

            norm_fact = _normalize_fact_text(fact)
            if not norm_fact:
                continue

            entity_name = _resolve_entity_name(fact, item.get("entities"), participants)
            source = str(item.get("source") or "ai_extracted")

            exact_existing_id: str | None = None
            near_dup_idx: int | None = None
            best_ratio = 0.0
            for idx, (ex_id, _ex_str, ex_norm) in enumerate(existing_items):
                if ex_norm == norm_fact:
                    exact_existing_id = ex_id
                    break
                ratio = difflib.SequenceMatcher(None, norm_fact, ex_norm).ratio()
                if ratio >= 0.88 and ratio > best_ratio:
                    best_ratio = ratio
                    near_dup_idx = idx

            if exact_existing_id is not None:
                self._sync_upsert_db(
                    chat_id,
                    exact_existing_id,
                    fact,
                    entity_name=entity_name,
                    source=source,
                    confidence=confidence,
                )
                continue

            if near_dup_idx is not None:
                doc_id = existing_items[near_dup_idx][0]
                existing_items[near_dup_idx] = (doc_id, fact, norm_fact)
            else:
                doc_id = f"fact_{chat_id}_{_md5_short(fact)}"
                existing_items.append((doc_id, fact, norm_fact))

            now_iso = datetime.now(timezone.utc).isoformat()
            self._collection.upsert(
                documents=[fact],
                metadatas=[
                    {
                        "chat_id": str(chat_id),
                        "entity_name": entity_name,
                        "source": source,
                        "confidence": confidence,
                        "updated_at": now_iso,
                    }
                ],
                ids=[doc_id],
            )
            self._sync_upsert_db(
                chat_id,
                doc_id,
                fact,
                entity_name=entity_name,
                source=source,
                confidence=confidence,
            )

            stored += 1

        if stored > 0:
            self._log("facts_stored", chat_id=chat_id, count=stored)
            if self.backup_service:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.backup_service.upload_backup(f"💾 Авто-бэкап памяти (+{stored} новых фактов)"))
                except RuntimeError:
                    pass

    async def _extract_memories(self, transcript: str, participants: list[str]) -> dict[str, Any] | None:
        if not self.client:
            return None

        messages = build_memory_extraction_messages(
            bot_name=self.settings.bot_name,
            transcript=transcript,
            participants=participants,
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.settings.effective_memory_model,
                messages=messages,
                temperature=0.1,
                max_tokens=500,
            )
            content = response.choices[0].message.content or ""
            return _parse_json_content(content)

        except Exception as e:
            self._log("extraction_error", error=str(e))
            return None

    def store_single_fact(
        self,
        chat_id: int,
        fact: str,
        entity_name: str = "",
        *,
        source_message_id: int | None = None,
        user_id: int | None = None,
        _sync_to_db: bool = True,
    ) -> bool:
        """Сохранение одного конкретного факта вручную (например через remember_user_fact)"""
        if not self._collection or not fact.strip():
            return False

        try:
            clean_fact = fact.strip()
            doc_id = f"fact_{chat_id}_{_md5_short(clean_fact)}"
            now_iso = datetime.now(timezone.utc).isoformat()
            meta: dict[str, Any] = {
                "chat_id": str(chat_id),
                "entity_name": entity_name,
                "source": "manual_remember",
                "updated_at": now_iso,
            }
            if source_message_id is not None:
                meta["source_message_id"] = int(source_message_id)
            if user_id is not None:
                meta["user_id"] = int(user_id)
            self._collection.upsert(
                documents=[clean_fact],
                metadatas=[meta],
                ids=[doc_id],
            )
            if _sync_to_db:
                self._sync_upsert_db(
                    chat_id,
                    doc_id,
                    clean_fact,
                    entity_name=entity_name,
                    source="manual_remember",
                    confidence=0.95,
                    source_message_id=source_message_id,
                    user_id=user_id,
                )
            self._log("single_fact_stored", chat_id=chat_id, fact=clean_fact[:60])
            if self.backup_service:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.backup_service.upload_backup("💾 Ручной бэкап памяти (новое воспоминание)"))
                except RuntimeError:
                    pass
            return True
        except Exception as e:
            self._log("store_single_fact_error", error=str(e))
            return False

    def delete_fact_by_source_id(self, chat_id: int, source_message_id: int) -> int:
        """Удаляет факты в ChromaDB, привязанные к source_message_id (кнопка 'Забыть этот факт')"""
        if not self._collection or not source_message_id:
            return 0
        try:
            results = self._collection.get(where={"chat_id": str(chat_id)})
            if not results or not results.get("ids"):
                return 0

            candidate_texts: set[str] = set()
            try:
                rows = self.db._safe_execute(
                    self.db._knowledge()
                    .select("fact, entity_name")
                    .eq("chat_id", chat_id)
                    .eq("source_message_id", source_message_id),
                    fallback=None,
                )
                if rows and rows.data:
                    for item in rows.data:
                        raw_fact = str(item.get("fact") or "").strip()
                        if raw_fact:
                            candidate_texts.add(raw_fact.casefold())
                            if ": " in raw_fact:
                                note_part = raw_fact.split(": ", 1)[1].strip()
                                entity = str(item.get("entity_name") or "").strip()
                                if entity and note_part:
                                    candidate_texts.add(f"{entity}: {note_part}".casefold())
            except Exception:
                pass

            ids_to_delete: list[str] = []
            for doc_id, doc, meta in zip(results["ids"], results["documents"], results["metadatas"]):
                meta = meta or {}
                if str(meta.get("source_message_id") or "") == str(source_message_id):
                    ids_to_delete.append(doc_id)
                elif doc and doc.strip().casefold() in candidate_texts:
                    ids_to_delete.append(doc_id)

            if ids_to_delete:
                self._collection.delete(ids=ids_to_delete)
                self._sync_delete_db(chat_id, ids_to_delete)
                if self.backup_service:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.backup_service.upload_backup("💾 Авто-бэкап после удаления факта кнопкой"))
                    except RuntimeError:
                        pass
            return len(ids_to_delete)
        except Exception as e:
            self._log("delete_fact_by_source_id_error", error=str(e))
            return 0

    def delete_fact_by_query(self, chat_id: int, query: str) -> int:
        """Удаление факта по подстроке"""
        if not self._collection:
            return 0

        try:
            results = self._collection.get(where={"chat_id": str(chat_id)})
            deleted_ids: list[str] = []
            if results and results.get("ids"):
                for doc_id, doc in zip(results["ids"], results["documents"]):
                    if query.casefold() in doc.casefold():
                        self._collection.delete(ids=[doc_id])
                        deleted_ids.append(doc_id)
            if deleted_ids:
                self._sync_delete_db(chat_id, deleted_ids)
                if self.backup_service:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.backup_service.upload_backup("💾 Бэкап после удаления факта"))
                    except RuntimeError:
                        pass
            return len(deleted_ids)
        except Exception as e:
            self._log("delete_fact_error", error=str(e))
            return 0

    def add_single_fact(self, chat_id: int, fact_text: str, entity_name: str = "") -> bool:
        """Ручное добавление факта админом"""
        if not self._collection or not fact_text.strip():
            return False
        clean_fact = fact_text.strip()
        doc_id = f"manual_{chat_id}_{_md5_short(clean_fact)}"
        now_iso = datetime.now(timezone.utc).isoformat()
        self._collection.upsert(
            documents=[clean_fact],
            metadatas=[{
                "chat_id": str(chat_id),
                "entity_name": entity_name,
                "source": "manual_admin",
                "confidence": 1.0,
                "updated_at": now_iso,
            }],
            ids=[doc_id]
        )
        self._sync_upsert_db(
            chat_id,
            doc_id,
            clean_fact,
            entity_name=entity_name,
            source="manual_admin",
            confidence=1.0,
        )
        if self.backup_service:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.backup_service.upload_backup("💾 Авто-бэкап (ручное добавление факта)"))
            except RuntimeError:
                pass
        return True

    def edit_fact_text(self, chat_id: int, old_text: str, new_text: str) -> int:
        """Редактирование/замена текста в фактах"""
        if not self._collection or not old_text.strip() or not new_text.strip():
            return 0
        all_data = self._collection.get(where={"chat_id": str(chat_id)})
        count = 0
        if all_data and all_data.get("documents"):
            now_iso = datetime.now(timezone.utc).isoformat()
            for doc_id, doc, meta in zip(all_data["ids"], all_data["documents"], all_data["metadatas"]):
                if old_text.casefold() in doc.casefold():
                    updated_doc = re.sub(re.escape(old_text), new_text, doc, flags=re.IGNORECASE)
                    updated_meta = {**(meta or {}), "updated_at": now_iso}
                    self._collection.upsert(
                        documents=[updated_doc],
                        metadatas=[updated_meta],
                        ids=[doc_id]
                    )
                    self._sync_update_db(chat_id, doc_id, updated_doc)
                    count += 1
        if count > 0 and self.backup_service:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.backup_service.upload_backup(f"💾 Авто-бэкап (изменено {count} фактов)"))
            except RuntimeError:
                pass
        return count

    def get_user_facts(
        self,
        chat_id: int,
        user_or_id: str | int = "",
        user_name_or_limit: str | int = 10,
        *,
        user_id: int | None = None,
        user_name: str = "",
        limit: int = 10,
    ) -> list[str]:
        """Возвращает факты, относящиеся к конкретному участнику (сначала самые свежие)"""
        if isinstance(user_or_id, int):
            resolved_user_id: int | None = user_or_id
            if isinstance(user_name_or_limit, str):
                resolved_user_name = user_name_or_limit
                resolved_limit = limit
            else:
                resolved_user_name = user_name
                resolved_limit = int(user_name_or_limit)
        else:
            resolved_user_id = user_id
            resolved_user_name = str(user_or_id or user_name).strip()
            resolved_limit = int(user_name_or_limit) if isinstance(user_name_or_limit, int) else limit

        if not resolved_user_name and resolved_user_id:
            try:
                u = self.db.get_user_by_platform_id(chat_id, resolved_user_id)
                if u:
                    resolved_user_name = u.display_name
            except Exception:
                pass

        if not self._collection:
            return []
        try:
            results = self._collection.get(where={"chat_id": str(chat_id)})
            facts: list[str] = []
            if results and results.get("documents"):
                clean_name = resolved_user_name.strip().casefold()
                for doc, meta in _iter_docs_newest_first(
                    results["documents"],
                    results.get("metadatas") or [],
                ):
                    entity = str(meta.get("entity_name") or "").casefold()
                    if resolved_user_id and meta.get("user_id") == resolved_user_id:
                        facts.append(doc)
                    elif clean_name and (entity == clean_name or clean_name in doc.casefold()):
                        facts.append(doc)
            return _clean_memory_items(list(dict.fromkeys(facts)))[:resolved_limit]
        except Exception as e:
            self._log("get_user_facts_error", error=str(e))
            return []

    def get_all_facts_paged(self, chat_id: int, page: int = 1, page_size: int = 5, query: str = "") -> dict[str, Any]:
        """Возвращает пагинированный список фактов с опциональной фильтрацией по участнику или тексту"""
        if not self._collection:
            return {"facts": [], "total": 0, "page": page, "pages": 1, "query": query}
        results = self._collection.get(where={"chat_id": str(chat_id)})
        facts = []
        clean_q = query.lstrip("@").strip().casefold() if query else ""
        if results and results.get("ids"):
            for doc_id, doc, meta in zip(results["ids"], results["documents"], results["metadatas"]):
                entity = str(meta.get("entity_name") or "")
                if not clean_q or clean_q in entity.casefold() or clean_q in doc.casefold():
                    facts.append({
                        "id": doc_id,
                        "text": doc,
                        "entity_name": entity,
                        "source": meta.get("source", "fact"),
                    })
        total = len(facts)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, pages))
        start = (page - 1) * page_size
        return {
            "facts": facts[start:start + page_size],
            "total": total,
            "page": page,
            "pages": pages,
            "query": query,
        }

    def delete_fact_by_id(self, chat_id: int, doc_id: str) -> bool:
        """Удаляет конкретный факт по его первичному ключу ID в ChromaDB"""
        if not self._collection:
            return False
        try:
            self._collection.delete(ids=[doc_id])
            self._sync_delete_db(chat_id, [doc_id])
            if self.backup_service:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.backup_service.upload_backup("💾 Авто-бэкап после удаления факта кнопкой"))
                except RuntimeError:
                    pass
            return True
        except Exception as e:
            self._log("delete_fact_by_id_error", error=str(e))
            return False

    def update_fact_text_by_id(self, chat_id: int, doc_id: str, new_text: str) -> bool:
        """Обновляет текст конкретного факта по его ID"""
        if not self._collection or not new_text.strip():
            return False
        try:
            results = self._collection.get(ids=[doc_id])
            if results and results.get("metadatas"):
                clean_new = new_text.strip()
                meta = {**(results["metadatas"][0] or {}), "updated_at": datetime.now(timezone.utc).isoformat()}
                self._collection.upsert(
                    documents=[clean_new],
                    metadatas=[meta],
                    ids=[doc_id],
                )
                self._sync_update_db(chat_id, doc_id, clean_new)
                if self.backup_service:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.backup_service.upload_backup("💾 Авто-бэкап после обновления факта кнопкой"))
                    except RuntimeError:
                        pass
                return True
            return False
        except Exception as e:
            self._log("update_fact_text_by_id_error", error=str(e))
            return False

    def cleanup_chat_memory(self, chat_id: int, *, full_wipe: bool = False) -> int:
        """Очищает ChromaDB от бытового мусора и дублей (или полностью для чата при full_wipe=True)."""
        if not self._collection:
            return 0
        try:
            all_data = self._collection.get(where={"chat_id": str(chat_id)})
            if not all_data or not all_data.get("ids"):
                return 0
            ids = list(all_data["ids"])
            docs = list(all_data.get("documents") or [])
            if full_wipe:
                self._collection.delete(ids=ids)
                self._sync_delete_db(chat_id, ids)
                return len(ids)

            seen: set[str] = set()
            to_delete: list[str] = []
            for doc_id, doc in zip(ids, docs):
                cleaned = _clean_legacy_fact(str(doc or ""))
                norm = cleaned.casefold()
                if not cleaned or len(cleaned) < 8 or norm in seen:
                    to_delete.append(doc_id)
                else:
                    seen.add(norm)
            if to_delete:
                self._collection.delete(ids=to_delete)
                self._sync_delete_db(chat_id, to_delete)
            return len(to_delete)
        except Exception as e:
            self._log("cleanup_chat_memory_error", error=str(e))
            return 0



