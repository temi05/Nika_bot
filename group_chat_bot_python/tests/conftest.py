from __future__ import annotations

import contextlib
import copy
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import CallbackQuery, Chat, Message, User
from postgrest.exceptions import APIError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "fake-supabase-key")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("WEBHOOK_SECRET_TOKEN", "test-webhook-secret")

from app.config import Settings
from app.models import ChatUser, Sender
from app.services.ai_service import AIService
from app.services.chroma_memory_provider import ChromaMemoryProvider
from app.services.memory_provider import DatabaseMemoryProvider
from app.services.persona_service import PersonaService
from app.services.supabase_db import SupabaseDB
from app.services.telegram_backup import TelegramBackupService


class FakeQueryResponse:
    def __init__(self, data: list[dict[str, Any]] | Any, count: int | None = None) -> None:
        self.data = data
        self.count = count


class FakeTableQueryBuilder:
    def __init__(self, client: FakeSupabaseClient, table_name: str) -> None:
        self.client = client
        self.table_name = table_name
        self._op: str = "select"
        self._select_cols: str = "*"
        self._count_mode: str | None = None
        self._payload: Any = None
        self._on_conflict: str | None = None
        self._filters: list[Any] = []
        self._order_col: str | None = None
        self._order_desc: bool = False
        self._limit_val: int | None = None

    def select(self, columns: str = "*", count: str | None = None) -> FakeTableQueryBuilder:
        self._op = "select"
        self._select_cols = columns
        self._count_mode = count
        return self

    def insert(self, payload: dict[str, Any] | list[dict[str, Any]]) -> FakeTableQueryBuilder:
        self._op = "insert"
        self._payload = copy.deepcopy(payload)
        return self

    def update(self, payload: dict[str, Any]) -> FakeTableQueryBuilder:
        self._op = "update"
        self._payload = copy.deepcopy(payload)
        return self

    def upsert(self, payload: dict[str, Any] | list[dict[str, Any]], on_conflict: str | None = None) -> FakeTableQueryBuilder:
        self._op = "upsert"
        self._payload = copy.deepcopy(payload)
        self._on_conflict = on_conflict
        return self

    def delete(self) -> FakeTableQueryBuilder:
        self._op = "delete"
        return self

    def eq(self, column: str, value: Any) -> FakeTableQueryBuilder:
        self._filters.append(lambda row: row.get(column) == value)
        return self

    def neq(self, column: str, value: Any) -> FakeTableQueryBuilder:
        self._filters.append(lambda row: row.get(column) != value)
        return self

    def gt(self, column: str, value: Any) -> FakeTableQueryBuilder:
        self._filters.append(lambda row: (row.get(column) is not None) and row.get(column) > value)
        return self

    def gte(self, column: str, value: Any) -> FakeTableQueryBuilder:
        self._filters.append(lambda row: (row.get(column) is not None) and row.get(column) >= value)
        return self

    def lt(self, column: str, value: Any) -> FakeTableQueryBuilder:
        self._filters.append(lambda row: (row.get(column) is not None) and row.get(column) < value)
        return self

    def lte(self, column: str, value: Any) -> FakeTableQueryBuilder:
        self._filters.append(lambda row: (row.get(column) is not None) and row.get(column) <= value)
        return self

    def ilike(self, column: str, pattern: str) -> FakeTableQueryBuilder:
        regex_pat = "^" + re.escape(pattern).replace(r"\%", ".*").replace(r"\_", ".") + "$"
        compiled = re.compile(regex_pat, flags=re.IGNORECASE | re.DOTALL)
        self._filters.append(lambda row: bool(compiled.match(str(row.get(column) or ""))))
        return self

    def in_(self, column: str, values: list[Any]) -> FakeTableQueryBuilder:
        val_set = set(values)
        self._filters.append(lambda row: row.get(column) in val_set)
        return self

    def order(self, column: str, desc: bool = False) -> FakeTableQueryBuilder:
        self._order_col = column
        self._order_desc = desc
        return self

    def limit(self, count: int) -> FakeTableQueryBuilder:
        self._limit_val = count
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        return all(pred(row) for pred in self._filters)

    def _validate_constraints(self, row: dict[str, Any]) -> None:
        if self.table_name == "bot_debts":
            status = row.get("status", "active")
            if status not in {"active", "repaid", "forgiven"}:
                raise APIError(
                    {
                        "message": f'new row for relation "bot_debts" violates check constraint "bot_debts_status_check" (status={status!r})',
                        "code": "23514",
                        "details": "Failing row contains invalid status.",
                        "hint": None,
                    }
                )

    def execute(self) -> FakeQueryResponse:
        self.client.query_counts[self.table_name] = self.client.query_counts.get(self.table_name, 0) + 1
        self.client.operations.append((self.table_name, self._op, copy.deepcopy(self._payload)))
        rows = self.client.tables.setdefault(self.table_name, [])

        if self._op == "select":
            matched = [copy.deepcopy(r) for r in rows if self._matches(r)]
            if self._order_col:
                col = self._order_col
                matched.sort(key=lambda r: (r.get(col) is not None, r.get(col)), reverse=self._order_desc)
            total_count = len(matched)
            if self._limit_val is not None:
                matched = matched[: self._limit_val]
            return FakeQueryResponse(matched, count=total_count if self._count_mode else None)

        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            inserted: list[dict[str, Any]] = []
            for item in items:
                new_row = copy.deepcopy(item)
                self._validate_constraints(new_row)
                if "id" not in new_row or new_row["id"] is None:
                    new_row["id"] = self.client.next_id(self.table_name)
                if "created_at" not in new_row:
                    new_row["created_at"] = datetime.now(timezone.utc).isoformat()
                rows.append(new_row)
                inserted.append(copy.deepcopy(new_row))
            return FakeQueryResponse(inserted)

        if self._op == "update":
            updated: list[dict[str, Any]] = []
            for row in rows:
                if self._matches(row):
                    candidate = {**row, **self._payload}
                    self._validate_constraints(candidate)
                    row.update(copy.deepcopy(self._payload))
                    updated.append(copy.deepcopy(row))
            return FakeQueryResponse(updated)

        if self._op == "upsert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            conflict_cols = [c.strip() for c in (self._on_conflict or "id").split(",") if c.strip()]
            if self.table_name == "bot_persona_state" and not self._on_conflict:
                # In Postgres, bot_persona_state has unique index on (chat_id, user_id),
                # so upsert without on_conflict="chat_id,user_id" when id is absent fails on duplicate (chat_id, user_id)
                for item in items:
                    if "id" not in item:
                        for existing in rows:
                            if existing.get("chat_id") == item.get("chat_id") and existing.get("user_id") == item.get("user_id"):
                                raise APIError(
                                    {
                                        "message": 'duplicate key value violates unique constraint "bot_persona_state_chat_user_uidx"',
                                        "code": "23505",
                                        "details": "Key (chat_id, user_id) already exists.",
                                        "hint": "Specify on_conflict='chat_id,user_id'",
                                    }
                                )
            upserted: list[dict[str, Any]] = []
            for item in items:
                new_item = copy.deepcopy(item)
                self._validate_constraints(new_item)
                match_row = None
                if conflict_cols:
                    for existing in rows:
                        if all(existing.get(c) is not None and existing.get(c) == new_item.get(c) for c in conflict_cols):
                            match_row = existing
                            break
                if match_row is not None:
                    match_row.update(new_item)
                    upserted.append(copy.deepcopy(match_row))
                else:
                    if "id" not in new_item or new_item["id"] is None:
                        new_item["id"] = self.client.next_id(self.table_name)
                    rows.append(new_item)
                    upserted.append(copy.deepcopy(new_item))
            return FakeQueryResponse(upserted)

        if self._op == "delete":
            deleted: list[dict[str, Any]] = []
            remaining: list[dict[str, Any]] = []
            for row in rows:
                if self._matches(row):
                    deleted.append(copy.deepcopy(row))
                else:
                    remaining.append(row)
            self.client.tables[self.table_name] = remaining
            return FakeQueryResponse(deleted)

        return FakeQueryResponse([])


class FakeRPCBuilder:
    def __init__(self, client: FakeSupabaseClient, fn_name: str, params: dict[str, Any]) -> None:
        self.client = client
        self.fn_name = fn_name
        self.params = copy.deepcopy(params)

    def execute(self) -> FakeQueryResponse:
        self.client.rpc_calls.append((self.fn_name, copy.deepcopy(self.params)))
        if self.fn_name == "increment_user_stat":
            uid = self.params.get("p_user_id")
            col = self.params.get("p_column")
            allowed_cols = {"casino_plays", "mine_plays", "total_messages", "aiimage_plays", "signai_plays"}
            if col not in allowed_cols:
                raise APIError({"message": f"Invalid stat column: {col}", "code": "42703", "details": None, "hint": None})
            for row in self.client.tables.setdefault("users", []):
                if row.get("id") == uid:
                    row[col] = int(row.get(col) or 0) + 1
            return FakeQueryResponse([])
        if self.fn_name == "get_user_badges":
            return FakeQueryResponse([])
        if self.fn_name == "award_achievement_by_code":
            return FakeQueryResponse([])
        return FakeQueryResponse([])


class FakeSupabaseClient:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self._id_counters: dict[str, int] = {}
        self.query_counts: dict[str, int] = {}
        self.operations: list[tuple[str, str, Any]] = []
        self.rpc_calls: list[tuple[str, dict[str, Any]]] = []

    def next_id(self, table_name: str) -> int:
        nxt = self._id_counters.get(table_name, 0) + 1
        self._id_counters[table_name] = nxt
        return nxt

    def table(self, table_name: str) -> FakeTableQueryBuilder:
        return FakeTableQueryBuilder(self, table_name)

    def rpc(self, fn_name: str, params: dict[str, Any] | None = None) -> FakeRPCBuilder:
        return FakeRPCBuilder(self, fn_name, params or {})


@contextlib.asynccontextmanager
async def _noop_typing(*args: Any, **kwargs: Any):
    yield


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        SUPABASE_URL="https://fake.supabase.co",
        SUPABASE_KEY="fake-supabase-key",
        OPENAI_API_KEY="sk-test-openai-key",
        WEBHOOK_SECRET_TOKEN="test-webhook-secret",
        BOT_NAME="НейроНика",
        BOT_USERNAME="NikaBot",
        MEMORY_BACKUP_CHAT_ID=-100999888,
        WARN_LIMIT=3,
    )


@pytest.fixture
def fake_supabase_client() -> FakeSupabaseClient:
    return FakeSupabaseClient()


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch, test_settings: Settings, fake_supabase_client: FakeSupabaseClient) -> SupabaseDB:
    import app.services.supabase_db as supabase_db_mod
    import app.bot.routers.chat_settings as chat_settings_mod

    monkeypatch.setattr(supabase_db_mod, "create_client", lambda url, key: fake_supabase_client)
    monkeypatch.setattr(supabase_db_mod.time, "sleep", lambda s: None)
    chat_settings_mod._settings_cache.clear()

    db = SupabaseDB(test_settings)
    db.fake_client = fake_supabase_client  # type: ignore[attr-defined]
    return db


@pytest.fixture
def mock_bot(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import app.bot.messages as messages_mod

    monkeypatch.setattr(messages_mod.ChatActionSender, "typing", _noop_typing)

    bot = MagicMock(spec=Bot)
    bot.id = 999000
    admin_map: set[tuple[int, int]] = set()
    global_admins: set[int] = set()
    msg_counter = {"id": 1000}

    def set_admin(chat_id: int, user_id: int, is_admin_flag: bool = True) -> None:
        if is_admin_flag:
            admin_map.add((chat_id, user_id))
            global_admins.add(user_id)
        else:
            admin_map.discard((chat_id, user_id))
            global_admins.discard(user_id)
        import app.bot.admin as admin_mod

        admin_mod.invalidate_admin_cache(chat_id, user_id)

    bot.set_admin = set_admin

    async def _get_me() -> Any:
        return SimpleNamespace(id=999000, username="NikaBot", first_name="НейроНика", is_bot=True)

    async def _get_chat_member(chat_id: int, user_id: int) -> Any:
        if (chat_id, user_id) in admin_map or user_id in global_admins:
            return SimpleNamespace(status="administrator", user=SimpleNamespace(id=user_id, is_bot=False))
        return SimpleNamespace(status="member", user=SimpleNamespace(id=user_id, is_bot=False))

    async def _send_message(chat_id: int | str, text: str = "", **kwargs: Any) -> Any:
        msg_counter["id"] += 1
        sent = MagicMock(spec=Message)
        sent.message_id = msg_counter["id"]
        sent.chat = SimpleNamespace(id=chat_id, type="supergroup", title="Test Chat")
        sent.text = text
        sent.edit_text = AsyncMock()
        sent.delete = AsyncMock()
        return sent

    async def _send_photo(chat_id: int | str, photo: Any = None, **kwargs: Any) -> Any:
        msg_counter["id"] += 1
        sent = MagicMock(spec=Message)
        sent.message_id = msg_counter["id"]
        sent.chat = SimpleNamespace(id=chat_id, type="supergroup", title="Test Chat")
        sent.caption = kwargs.get("caption", "")
        sent.edit_text = AsyncMock()
        sent.delete = AsyncMock()
        return sent

    bot.get_me = AsyncMock(side_effect=_get_me)
    bot.get_chat_member = AsyncMock(side_effect=_get_chat_member)
    bot.send_message = AsyncMock(side_effect=_send_message)
    bot.send_photo = AsyncMock(side_effect=_send_photo)
    bot.send_document = AsyncMock(side_effect=_send_message)
    bot.send_animation = AsyncMock(side_effect=_send_message)
    bot.send_sticker = AsyncMock(side_effect=_send_message)
    bot.send_dice = AsyncMock(side_effect=_send_message)
    bot.send_poll = AsyncMock(
        return_value=SimpleNamespace(message_id=7777, poll=SimpleNamespace(id="poll_test_1"))
    )
    bot.restrict_chat_member = AsyncMock(return_value=True)
    bot.ban_chat_member = AsyncMock(return_value=True)
    bot.unban_chat_member = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    bot.delete_messages = AsyncMock(return_value=True)
    bot.pin_chat_message = AsyncMock(return_value=True)
    bot.unpin_chat_message = AsyncMock(return_value=True)
    bot.set_message_reaction = AsyncMock(return_value=True)
    bot.set_webhook = AsyncMock(return_value=True)
    bot.set_my_commands = AsyncMock(return_value=True)
    bot.send_chat_action = AsyncMock(return_value=True)
    bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="test/path.dat", file_size=128))
    bot.download_file = AsyncMock(return_value=b"test_payload")
    bot.session = SimpleNamespace(close=AsyncMock())
    return bot


@pytest.fixture
def backup_service(mock_bot: MagicMock, tmp_path: Path) -> TelegramBackupService:
    data_dir = tmp_path / "chroma_db"
    data_dir.mkdir(parents=True, exist_ok=True)
    return TelegramBackupService(mock_bot, -100999888, data_dir)


@pytest.fixture
def chroma_memory(
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
    fake_db: SupabaseDB,
    backup_service: TelegramBackupService,
    tmp_path: Path,
) -> ChromaMemoryProvider:
    import chromadb

    real_persistent_client = chromadb.PersistentClient
    isolated_dir = tmp_path / "chroma_db"
    isolated_dir.mkdir(parents=True, exist_ok=True)

    def _redirected_persistent_client(path: str = "", *args: Any, **kwargs: Any) -> Any:
        return real_persistent_client(path=str(isolated_dir), *args, **kwargs)

    monkeypatch.setattr(chromadb, "PersistentClient", _redirected_persistent_client)
    provider = ChromaMemoryProvider(test_settings, fake_db, backup_service)
    provider.data_dir = isolated_dir
    provider.client = MagicMock()
    provider.client.chat = MagicMock()
    provider.client.chat.completions = MagicMock()
    provider.client.chat.completions.create = AsyncMock()
    return provider


@pytest.fixture
def database_memory(test_settings: Settings, fake_db: SupabaseDB) -> DatabaseMemoryProvider:
    provider = DatabaseMemoryProvider(test_settings, fake_db)
    provider.client = MagicMock()
    provider.client.chat = MagicMock()
    provider.client.chat.completions = MagicMock()
    provider.client.chat.completions.create = AsyncMock()
    return provider


@pytest.fixture
def persona_service(fake_db: SupabaseDB) -> PersonaService:
    return PersonaService(fake_db)


@pytest.fixture
def ai_service(
    test_settings: Settings,
    fake_db: SupabaseDB,
    chroma_memory: ChromaMemoryProvider,
    persona_service: PersonaService,
    mock_bot: MagicMock,
) -> AIService:
    service = AIService(test_settings, fake_db, chroma_memory, persona_service, mock_bot)
    service.client = MagicMock()
    service.client.chat = MagicMock()
    service.client.chat.completions = MagicMock()
    service.client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Тестовый ответ Ники!", tool_calls=[]),
                    finish_reason="stop",
                )
            ]
        )
    )
    service.image_client = MagicMock()
    service.image_client.images = MagicMock()
    service.image_client.images.generate = AsyncMock()
    service.image_client.images.edit = AsyncMock()
    return service


def make_test_message(
    mock_bot: MagicMock,
    text: str | None = "",
    *,
    chat_id: int = -1001234567890,
    chat_type: str = "supergroup",
    chat_title: str = "NeuroNika Group",
    user_id: int = 1001,
    first_name: str = "Алексей",
    username: str | None = "alex_dev",
    is_bot: bool = False,
    message_id: int = 501,
    reply_to_message: Any = None,
    entities: list[Any] | None = None,
    caption: str | None = None,
    caption_entities: list[Any] | None = None,
    new_chat_members: list[Any] | None = None,
    document: Any = None,
    photo: Any = None,
    sticker: Any = None,
    forward_date: Any = None,
    sender_chat: Any = None,
) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.text = text
    msg.caption = caption
    msg.entities = entities or []
    msg.caption_entities = caption_entities or []
    msg.chat = SimpleNamespace(id=chat_id, type=chat_type, title=chat_title)
    msg.from_user = SimpleNamespace(
        id=user_id,
        first_name=first_name,
        full_name=first_name,
        username=username,
        is_bot=is_bot,
    )
    msg.sender_chat = sender_chat
    msg.reply_to_message = reply_to_message
    msg.new_chat_members = new_chat_members
    msg.left_chat_member = None
    msg.pinned_message = None
    msg.new_chat_title = None
    msg.photo = photo
    msg.sticker = sticker
    msg.video = None
    msg.animation = None
    msg.voice = None
    msg.video_note = None
    msg.audio = None
    msg.document = document
    msg.forward_date = forward_date
    msg.bot = mock_bot

    async def _answer(ans_text: str = "", **kwargs: Any) -> Any:
        sent = MagicMock(spec=Message)
        sent.message_id = message_id + 100
        sent.chat = msg.chat
        sent.text = ans_text
        sent.reply_markup = kwargs.get("reply_markup")
        sent.edit_text = AsyncMock()
        sent.delete = AsyncMock()
        return sent

    msg.answer = AsyncMock(side_effect=_answer)
    msg.reply = AsyncMock(side_effect=_answer)
    msg.delete = AsyncMock(return_value=True)
    msg.edit_text = AsyncMock(side_effect=_answer)
    msg.answer_photo = AsyncMock(side_effect=_answer)
    msg.answer_document = AsyncMock(side_effect=_answer)
    return msg


def make_test_callback(
    mock_bot: MagicMock,
    data: str,
    *,
    chat_id: int = -1001234567890,
    user_id: int = 1001,
    first_name: str = "Алексей",
    username: str | None = "alex_dev",
    is_bot: bool = False,
    message_id: int = 600,
    message_text: str = "",
) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.id = f"cb_{message_id}_{user_id}"
    cb.data = data
    cb.from_user = SimpleNamespace(
        id=user_id,
        first_name=first_name,
        full_name=first_name,
        username=username,
        is_bot=is_bot,
    )
    cb.message = make_test_message(
        mock_bot,
        text=message_text,
        chat_id=chat_id,
        user_id=999000,
        first_name="НейроНика",
        username="NikaBot",
        is_bot=True,
        message_id=message_id,
    )
    cb.bot = mock_bot
    cb.answer = AsyncMock(return_value=True)
    return cb


@pytest.fixture
def message_factory(mock_bot: MagicMock):
    def _factory(text: str | None = "", **kwargs: Any) -> MagicMock:
        return make_test_message(mock_bot, text, **kwargs)
    return _factory


@pytest.fixture
def callback_factory(mock_bot: MagicMock):
    def _factory(data: str, **kwargs: Any) -> MagicMock:
        return make_test_callback(mock_bot, data, **kwargs)
    return _factory
