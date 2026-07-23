from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.models import MemoryRecord
from app.services.memory_provider import BaseMemoryProvider, _compact_transcript, _clean_memory_items, _parse_json_content
from app.services.prompt_builders import build_memory_extraction_messages, format_memory_context
from app.services.supabase_db import SupabaseDB
from app.services.telegram_backup import TelegramBackupService


class LightweightEmbeddingFunction:
    """Легковесный математический векторный эмбеддер для экономии RAM на Render (<20 MB)"""
    def __init__(self, dim: int = 128) -> None:
        self.dim = dim

    def name(self) -> str:
        return "lightweight_embedding_function"

    def embed_documents(self, input: list[str]) -> list[list[float]]:
        embeddings = []
        for text in input:
            tokens = re.findall(r"\w+", text.lower())
            vec = [0.0] * self.dim
            for token in tokens:
                idx = abs(hash(token)) % self.dim
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
        "недопонимание", "эмоция:", "состояние:", "состояние "
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

    name_map = {
        r"\bДанила\b": "Danil (@Markvannes)",
        r"\bDanil\b(?!\s*\(@)": "Danil (@Markvannes)",
        r"\bЧика\b(?!\s*\(@)": "Чика (@SCTemi)",
        r"\bsanechk_aaa\b(?!\s*\(@)": "sanechk_aaa (@sanechkkk_aaa)",
        r"\bСанечка\b": "sanechk_aaa (@sanechkkk_aaa)",
        r"\bЛексон⁴²\b(?!\s*\(@)": "Лексон⁴² (@LeksikN6)",
        r"\bЛюбимый\b(?!\s*\(@)": "Любимый (@Lubimbi_director)",
    }
    for pattern, repl in name_map.items():
        cleaned = re.sub(pattern, repl, cleaned)

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

    def _log(self, event: str, **kwargs: Any) -> None:
        details = " ".join(f"{key}={value!r}" for key, value in kwargs.items())
        print(f"[CHROMA_MEMORY:{event}] {details}".strip())

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



    def _migrate_if_needed(self) -> None:
        """Однократный перенос старых фактов из Supabase bot_knowledge в ChromaDB при пустой базе"""
        if not self._collection or self._collection.count() > 0 or self._migrated_from_supabase:
            return

        try:
            self._log("migration_start", status="fetching_from_supabase")
            records = self.db._knowledge().select("id,chat_id,fact,entity_name,fact_type,confidence").execute()
            if not records.data:
                self._log("migration_empty", reason="no_records_in_supabase")
                self._migrated_from_supabase = True
                return

            documents = []
            metadatas = []
            ids = []

            for idx, item in enumerate(records.data):
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



                chat_id = str(item.get("chat_id") or "0")
                entity_name = str(item.get("entity_name") or "")
                source = str(item.get("fact_type") or "fact")
                confidence = float(item.get("confidence") or 0.55)

                documents.append(fact_text)
                metadatas.append(
                    {
                        "chat_id": chat_id,
                        "entity_name": entity_name,
                        "source": source,
                        "confidence": confidence,
                    }
                )
                ids.append(f"supa_{item.get('id') or idx}")



            if documents:
                self._collection.add(documents=documents, metadatas=metadatas, ids=ids)
                self._log("migration_success", imported_count=len(documents))
                self._migrated_from_supabase = True
                if self.backup_service:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.backup_service.upload_backup("💾 Первичный бэкап с 939 фактами из Supabase"))
                    except RuntimeError:
                        pass
        except Exception as e:
            self._log("migration_error", error=str(e))


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
            user_facts: list[str] = []
            mentioned_facts: list[str] = []

            all_data = self._collection.get(where={"chat_id": str(chat_id)})
            if all_data and all_data.get("documents"):
                docs = all_data["documents"]
                metas = all_data["metadatas"]

                # 1. Факты об отправителе
                for doc, meta in zip(docs, metas):
                    if meta.get("entity_name") == user_name or user_name.casefold() in doc.casefold():
                        user_facts.append(doc)

                # 2. Точный поиск фактов об упомянутых участниках/сущностях в сообщении
                raw_words = [w.strip(" .,!?:;\"'()[]{}<>-").casefold() for w in re.findall(r"\b[\w\d_@]+\b", user_message)]
                stop_words = {"расскажи", "что", "про", "тебя", "меня", "кого", "какое", "знаешь", "есть", "тогда", "скажи", "какой", "какая", "почему", "зачем", "когда", "где", "как", "еще", "ещё"}
                target_words = [w for w in raw_words if len(w) >= 3 and w not in stop_words]

                for doc, meta in zip(docs, metas):
                    doc_lower = doc.casefold()
                    entity_lower = str(meta.get("entity_name") or "").casefold()
                    for word in target_words:
                        # Снимаем падежные окончания для поиска основы русской формы (например, псинке -> псинк)
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

            # Объединяем факты (упомянутые лица сначала, потом отправитель, потом семантика)
            combined = _clean_memory_items(list(dict.fromkeys(mentioned_facts[:6] + user_facts[:4] + semantic_facts[:4])))
            return format_memory_context(
                profile_facts=combined[:8],
                topic_facts=[],
                recent_facts=[],
            )
        except Exception as e:
            self._log("get_facts_error", error=str(e))
            return ""


    async def save_transcript(self, chat_id: int, transcript: str, participants: list[str]) -> None:
        compact = _compact_transcript(transcript)
        if not compact or not self._collection:
            return

        extracted = await self._extract_memories(compact, participants)
        if not extracted:
            return

        stored = 0
        for item in extracted.get("facts", [])[: self.settings.memory_extraction_max_facts]:
            fact = str(item.get("fact") or "").strip()
            if not fact:
                continue

            confidence = float(item.get("confidence") or 0.0)
            if confidence < self.settings.memory_fact_min_confidence:
                continue

            # Умный поиск дубликатов по смыслу перед записью
            entity_name = (item.get("entities") or [participants[0] if participants else ""])[0]
            doc_id = f"fact_{chat_id}_{hash(fact) & 0xFFFFFFFF}"
            
            try:
                existing = self._collection.query(
                    query_texts=[fact],
                    n_results=1,
                    where={"chat_id": str(chat_id)}
                )
                if existing and existing.get("distances") and existing["distances"][0]:
                    dist = existing["distances"][0][0]
                    if dist < 0.15 and existing.get("ids") and existing["ids"][0]:
                        doc_id = existing["ids"][0][0] # Перезаписываем существующий аналогичный факт
            except Exception:
                pass

            self._collection.upsert(
                documents=[fact],
                metadatas=[
                    {
                        "chat_id": str(chat_id),
                        "entity_name": entity_name,
                        "source": str(item.get("source") or "ai_extracted"),
                        "confidence": confidence,
                    }
                ],
                ids=[doc_id],
            )

            stored += 1

        if stored > 0:
            self._log("facts_stored", chat_id=chat_id, count=stored)
            if self.backup_service:
                asyncio.create_task(self.backup_service.upload_backup(f"💾 Авто-бэкап памяти (+{stored} новых фактов)"))

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

    def store_single_fact(self, chat_id: int, fact: str, entity_name: str = "") -> bool:
        """Сохранение одного конкретного факта вручную (например через remember_user_fact)"""
        if not self._collection or not fact.strip():
            return False

        try:
            clean_fact = fact.strip()
            doc_id = f"fact_{chat_id}_{hash(clean_fact) & 0xFFFFFFFF}"
            self._collection.upsert(
                documents=[clean_fact],
                metadatas=[
                    {
                        "chat_id": str(chat_id),
                        "entity_name": entity_name,
                        "source": "manual_remember",
                    }
                ],
                ids=[doc_id],
            )
            self._log("single_fact_stored", chat_id=chat_id, fact=clean_fact[:60])
            if self.backup_service:
                asyncio.create_task(self.backup_service.upload_backup("💾 Ручной бэкап памяти (новое воспоминание)"))
            return True
        except Exception as e:
            self._log("store_single_fact_error", error=str(e))
            return False

    def delete_fact_by_query(self, chat_id: int, query: str) -> int:
        """Удаление факта по подстроке"""
        if not self._collection:
            return 0

        try:
            results = self._collection.get(where={"chat_id": str(chat_id)})
            deleted = 0
            if results and results.get("ids"):
                for doc_id, doc in zip(results["ids"], results["documents"]):
                    if query.casefold() in doc.casefold():
                        self._collection.delete(ids=[doc_id])
                        deleted += 1
            if deleted > 0 and self.backup_service:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.backup_service.upload_backup("💾 Бэкап после удаления факта"))
                except RuntimeError:
                    pass
            return deleted
        except Exception as e:
            self._log("delete_fact_error", error=str(e))
            return 0

    def add_single_fact(self, chat_id: int, fact_text: str, entity_name: str = "") -> bool:
        """Ручное добавление факта админом"""
        if not self._collection or not fact_text.strip():
            return False
        doc_id = f"manual_{chat_id}_{hash(fact_text) & 0xFFFFFFFF}"
        self._collection.upsert(
            documents=[fact_text.strip()],
            metadatas=[{
                "chat_id": str(chat_id),
                "entity_name": entity_name,
                "source": "manual_admin",
                "confidence": 1.0,
            }],
            ids=[doc_id]
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
            for doc_id, doc, meta in zip(all_data["ids"], all_data["documents"], all_data["metadatas"]):
                if old_text.casefold() in doc.casefold():
                    updated_doc = re.sub(re.escape(old_text), new_text, doc, flags=re.IGNORECASE)
                    self._collection.upsert(
                        documents=[updated_doc],
                        metadatas=[meta],
                        ids=[doc_id]
                    )
                    count += 1
        if count > 0 and self.backup_service:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.backup_service.upload_backup(f"💾 Авто-бэкап (изменено {count} фактов)"))
            except RuntimeError:
                pass
        return count

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
                meta = results["metadatas"][0]
                self._collection.upsert(
                    documents=[new_text.strip()],
                    metadatas=[meta],
                    ids=[doc_id],
                )
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



