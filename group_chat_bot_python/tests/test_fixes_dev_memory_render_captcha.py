from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from fastapi.testclient import TestClient
from PIL import Image

import app.bot.admin as admin_mod
import app.bot.routers.general_admin as general_admin_mod
import app.bot.routers.jail_helpers as jail_helpers_mod
import app.bot.routers.profile_ai as profile_ai_mod
import app.config as config_mod
import app.web as web_mod
from app.config import Settings
from app.models import Sender, VerificationChallenge
from app.services.chroma_memory_provider import ChromaMemoryProvider
from app.services.prompt_builders import build_character_system_prompt
from app.services.supabase_db import SupabaseDB
from app.utils import build_captcha_image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent


# ==============================================================================
# 1. Developer / Superadmin Full Access Without Telegram Group Admin Rights
# ==============================================================================


@pytest.mark.asyncio
async def test_developer_access_by_id_and_username_without_chat_admin(
    monkeypatch: pytest.MonkeyPatch,
    mock_bot: MagicMock,
    test_settings: Settings,
) -> None:
    dev_settings = test_settings.model_copy(
        update={
            "superadmin_ids": [861713427, 424242],
            "developer_usernames": ["nika_creator"],
        }
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: dev_settings)
    admin_mod.invalidate_admin_cache()

    chat_id = -100777111
    # Neither ID 424242 nor @nika_creator is a Telegram chat admin
    mock_bot.set_admin(chat_id, 424242, False)
    mock_bot.set_admin(chat_id, 989898, False)
    mock_bot.set_admin(chat_id, 555000, False)

    assert admin_mod.is_superadmin(424242) is True
    assert admin_mod.is_superadmin(989898, "Nika_Creator") is True
    assert admin_mod.is_superadmin(989898, "@nika_creator") is True
    assert admin_mod.is_superadmin(555000, "ordinary_member") is False

    assert await admin_mod.is_admin(mock_bot, chat_id, 424242) is True
    assert await admin_mod.is_admin(mock_bot, chat_id, 989898, "Nika_Creator") is True
    assert await admin_mod.is_admin(mock_bot, chat_id, 555000, "ordinary_member") is False


@pytest.mark.asyncio
async def test_whoami_and_status_commands_for_developer_vs_regular_user(
    monkeypatch: pytest.MonkeyPatch,
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    dev_id = 424242
    regular_id = 515151
    chat_id = -100777222

    dev_settings = test_settings.model_copy(update={"superadmin_ids": [861713427, dev_id]})
    monkeypatch.setattr(config_mod, "get_settings", lambda: dev_settings)
    admin_mod.invalidate_admin_cache()
    web_mod._SUPERADMIN_SCOPED_CHATS.clear()
    mock_bot.set_my_commands = AsyncMock(return_value=True)

    mock_bot.set_admin(chat_id, dev_id, False)
    mock_bot.set_admin(chat_id, regular_id, False)

    dp = Dispatcher()
    dp.include_router(general_admin_mod.build_general_admin_router(fake_db, "НейроНика", ai_service))

    # 1. Developer runs /whoami -> recognized as developer & syncs personal slash-command scope
    msg_whoami_dev = message_factory("/whoami", chat_id=chat_id, user_id=dev_id, username="dev_master")
    await dp.propagate_event(update_type="message", event=msg_whoami_dev, bot=mock_bot)
    assert msg_whoami_dev.answer.called
    dev_whoami_text = msg_whoami_dev.answer.call_args.args[0]
    assert "Разработчик бота" in dev_whoami_text
    assert str(dev_id) in dev_whoami_text
    assert mock_bot.set_my_commands.called

    # 2. Regular user runs /whoami -> gets their ID + SUPERADMIN_IDS hint
    msg_whoami_reg = message_factory("/whoami", chat_id=chat_id, user_id=regular_id, username="regular_joe")
    await dp.propagate_event(update_type="message", event=msg_whoami_reg, bot=mock_bot)
    reg_whoami_text = msg_whoami_reg.answer.call_args.args[0]
    assert "Участник" in reg_whoami_text
    assert "SUPERADMIN_IDS" in reg_whoami_text

    # 3. Regular user runs /status -> denied
    msg_status_reg = message_factory("/status", chat_id=chat_id, user_id=regular_id, username="regular_joe")
    await dp.propagate_event(update_type="message", event=msg_status_reg, bot=mock_bot)
    assert "только администраторам и разработчику" in msg_status_reg.answer.call_args.args[0]

    # 4. Developer runs /status without chat admin -> full technical diagnostics returned
    msg_status_dev = message_factory("/status", chat_id=chat_id, user_id=dev_id, username="dev_master")
    await dp.propagate_event(update_type="message", event=msg_status_dev, bot=mock_bot)
    status_text = msg_status_dev.answer.call_args.args[0]
    assert "Технический статус" in status_text
    assert "Разработчик (Superadmin)" in status_text
    assert "Anti-sleep Render" in status_text


@pytest.mark.asyncio
async def test_developer_bypasses_ai_disabled_cookie_prices_jail_and_cooldowns(
    monkeypatch: pytest.MonkeyPatch,
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    dev_id = 424242
    chat_id = -100777333
    dev_settings = test_settings.model_copy(update={"superadmin_ids": [861713427, dev_id]})
    monkeypatch.setattr(config_mod, "get_settings", lambda: dev_settings)
    admin_mod.invalidate_admin_cache()
    mock_bot.set_admin(chat_id, dev_id, False)

    # Turn OFF ai_enabled in chat settings and set developer balance to 0 cookies + jailed!
    fake_db.update_chat_settings(chat_id, ai_enabled=False)
    dev_sender = Sender(user_id=dev_id, first_name="Creator", username="creator_dev")
    dev_user = fake_db.get_or_create_user(chat_id, dev_sender)
    fake_db.update_user(dev_user.id, {"reputation": 0})
    jail_helpers_mod.jail_user(fake_db, dev_user, 60, "тест")
    dev_user = fake_db.get_user_by_platform_id(chat_id, dev_id) or dev_user

    # Jail bypass check
    dev_msg_probe = message_factory("/daily", chat_id=chat_id, user_id=dev_id, username="creator_dev")
    assert await jail_helpers_mod.deny_if_jailed(fake_db, dev_msg_probe, dev_user, "/daily") is False

    # Mock image generation & Q&A on ai_service
    ai_service.generate_image = AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfake")
    ai_service.answer_question = AsyncMock(return_value="Точный ответ для создателя!")
    ai_service.summarize_chat = AsyncMock(return_value="Краткая сводка чата.")

    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))
    dp.include_router(general_admin_mod.build_general_admin_router(fake_db, "НейроНика", ai_service))

    # /aiimage works with 0 cookies and ai_enabled=False!
    msg_img = message_factory("/aiimage киберпанк город", chat_id=chat_id, user_id=dev_id, username="creator_dev")
    await dp.propagate_event(update_type="message", event=msg_img, bot=mock_bot)
    await asyncio.sleep(0.05)
    assert ai_service.generate_image.called
    assert msg_img.answer_photo.called

    # /ask works with ai_enabled=False and passes caller_is_owner=True!
    msg_ask = message_factory("/ask Как дела с базой?", chat_id=chat_id, user_id=dev_id, username="creator_dev")
    await dp.propagate_event(update_type="message", event=msg_ask, bot=mock_bot)
    assert ai_service.answer_question.called
    assert ai_service.answer_question.call_args.kwargs.get("caller_is_owner") is True

    # /drop can be triggered twice in a row by developer without 20-minute cooldown!
    msg_drop_1 = message_factory("/drop", chat_id=chat_id, user_id=dev_id, username="creator_dev")
    msg_drop_2 = message_factory("/drop", chat_id=chat_id, user_id=dev_id, username="creator_dev")
    await dp.propagate_event(update_type="message", event=msg_drop_1, bot=mock_bot)
    await dp.propagate_event(update_type="message", event=msg_drop_2, bot=mock_bot)
    assert "Печенька упала" in msg_drop_1.answer.call_args.args[0]
    assert "Печенька упала" in msg_drop_2.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_creator_prompt_and_low_mood_tool_bypass(
    ai_service: Any,
    fake_db: SupabaseDB,
) -> None:
    chat_id = -100777444
    prompt_owner = build_character_system_prompt(
        bot_name="НейроНика",
        user_name="@dev_owner",
        caller_is_admin=True,
        mood=20,
        persona_state={"stage": "close", "respect": 0.9},
        memory_context="",
        personality_mode="hard",
        compact_prompt=True,
        caller_is_owner=True,
    )
    assert "создатель" in prompt_owner.lower() and "разработчик" in prompt_owner.lower()

    # Force Nika's mood down to 10 (< 35)
    ai_service.moods[chat_id] = 10
    owner_sender = Sender(user_id=861713427, first_name="Temi", username="dev_owner")
    regular_sender = Sender(user_id=909090, first_name="User", username="user90")
    target_sender = Sender(user_id=919191, first_name="Friend", username="friend91")

    owner_db = fake_db.get_or_create_user(chat_id, owner_sender)
    reg_db = fake_db.get_or_create_user(chat_id, regular_sender)
    fake_db.get_or_create_user(chat_id, target_sender)
    fake_db.update_user(owner_db.id, {"reputation": 500})
    fake_db.update_user(reg_db.id, {"reputation": 500})

    # Regular user is refused transfer when mood=10
    reg_transfer = await ai_service._tool_transfer_reputation(
        chat_id,
        regular_sender,
        {"target_name": "@friend91", "amount": 20},
        caller_is_owner=False,
    )
    assert "слишком раздражена" in reg_transfer

    # Developer/owner succeeds even when mood=10
    owner_transfer = await ai_service._tool_transfer_reputation(
        chat_id,
        owner_sender,
        {"target_name": "@friend91", "amount": 20},
        caller_is_owner=True,
    )
    assert "слишком раздражена" not in owner_transfer
    assert "ТРАНЗАКЦИЯ ВЫПОЛНЕНА" in owner_transfer
    updated_friend = fake_db.get_user_by_platform_id(chat_id, 919191)
    assert updated_friend is not None and updated_friend.reputation >= 17


@pytest.mark.asyncio
async def test_developer_ban_gives_clear_hint_when_bot_lacks_telegram_admin_rights(
    fake_db: SupabaseDB,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    chat_id = -100777555
    dev_id = 861713427
    target_id = 333444
    mock_bot.set_admin(chat_id, dev_id, False)
    mock_bot.set_admin(chat_id, target_id, False)
    mock_bot.ban_chat_member = AsyncMock(side_effect=RuntimeError("Bad Request: not enough rights to restrict/ban chat member"))

    dp = Dispatcher()
    dp.include_router(admin_mod.build_admin_router(mock_bot, fake_db))

    reply_target = message_factory("спам ссылка", chat_id=chat_id, user_id=target_id, username="spammer")
    msg_ban = message_factory(
        "/ban спам",
        chat_id=chat_id,
        user_id=dev_id,
        username="dev_owner",
        reply_to_message=reply_target,
    )
    await dp.propagate_event(update_type="message", event=msg_ban, bot=mock_bot)
    err_reply = msg_ban.answer.call_args.args[0]
    assert "самому боту" in err_reply


# ==============================================================================
# 2. ChromaDB Memory Persistence, Deduplication & Retrieval Fixes
# ==============================================================================


@pytest.mark.asyncio
async def test_chromadb_does_not_overwrite_distinct_facts_and_updates_near_duplicates(
    chroma_memory: ChromaMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_id = -100888111

    # Batch 1: 3 distinct facts sharing overlapping tokens about the same participant
    batch_1 = {
        "facts": [
            {"fact": "Алексей работает backend-разработчиком на Python в финтехе", "confidence": 0.92, "entities": ["Алексей"]},
            {"fact": "Алексей работает над собственным игровым движком на Rust по выходным", "confidence": 0.91, "entities": ["Алексей"]},
            {"fact": "Алексей обожает пить фильтр-кофе из Эфиопии каждое утро", "confidence": 0.89, "entities": ["Алексей"]},
        ]
    }
    monkeypatch.setattr(chroma_memory, "_extract_memories", AsyncMock(return_value=batch_1))
    await chroma_memory.save_transcript(chat_id, "Алексей рассказал о работе и хобби", ["Алексей"])

    paged_after_1 = chroma_memory.get_all_facts_paged(chat_id, page=1, page_size=10)
    assert paged_after_1["total"] == 3, "All 3 distinct facts must be preserved without overwriting each other"

    # Batch 2: 1 near-duplicate of fact #3 + 1 brand-new fact #4
    batch_2 = {
        "facts": [
            {"fact": "Алексей обожает пить фильтр кофе из Эфиопии каждое утро!", "confidence": 0.95, "entities": ["Алексей"]},
            {"fact": "Алексей завёл рыжего кота по кличке Пиксель", "confidence": 0.93, "entities": ["Алексей"]},
        ]
    }
    monkeypatch.setattr(chroma_memory, "_extract_memories", AsyncMock(return_value=batch_2))
    await chroma_memory.save_transcript(chat_id, "Алексей рассказал про кота", ["Алексей"])

    paged_after_2 = chroma_memory.get_all_facts_paged(chat_id, page=1, page_size=10)
    assert paged_after_2["total"] == 4, "Near-duplicate must update in-place while new fact #4 is added (total=4)"
    all_texts = " | ".join(f["text"] for f in paged_after_2["facts"])
    assert "Python" in all_texts
    assert "Rust" in all_texts
    assert "Эфиопии" in all_texts
    assert "Пиксель" in all_texts


@pytest.mark.asyncio
async def test_flush_passive_memory_only_sends_unprocessed_messages(
    ai_service: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_id = -100888222
    captured_transcripts: list[str] = []

    async def _fake_save_transcript(cid: int, transcript: str, participants: list[str]) -> None:
        captured_transcripts.append(transcript)

    monkeypatch.setattr(ai_service.memory, "save_transcript", _fake_save_transcript)
    sender = Sender(user_id=701, first_name="Мария", username="maria")

    ai_service.remember_message(chat_id, sender, "Я переехала в Прагу в прошлом месяце")
    ai_service.remember_message(chat_id, sender, "Учу чешский язык и хожу в бассейн")
    await ai_service.flush_passive_memory(chat_id, force=True)

    assert len(captured_transcripts) == 1
    assert "Прагу" in captured_transcripts[0]

    # Calling flush_passive_memory again with NO new messages must NOT re-extract old tail!
    await ai_service.flush_passive_memory(chat_id, force=True)
    assert len(captured_transcripts) == 1

    # Adding 1 new message flushes ONLY the new message, while rolling chat history remains intact
    ai_service.remember_message(chat_id, sender, '<msg author="@maria" user_id="701">Моя любимая книга — Мастер и Маргарита</msg>')
    await ai_service.flush_passive_memory(chat_id, force=True)
    assert len(captured_transcripts) == 2
    assert "Мастер и Маргарита" in captured_transcripts[1]
    assert "Прагу" not in captured_transcripts[1]
    assert len(ai_service.chat_buffers[chat_id]) == 3


@pytest.mark.asyncio
async def test_chromadb_two_way_supabase_sync_and_cold_start_recovery(
    tmp_path: Path,
    fake_db: SupabaseDB,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_id = -100888333

    # Instance 1 writes extracted, manual, event, and deleted facts
    dir_1 = tmp_path / "chroma_instance_1"
    monkeypatch.setattr(
        ChromaMemoryProvider,
        "_init_chroma",
        ChromaMemoryProvider._init_chroma,
    )
    orig_init = ChromaMemoryProvider.__init__

    def _make_provider(target_dir: Path) -> ChromaMemoryProvider:
        provider = object.__new__(ChromaMemoryProvider)
        provider.settings = test_settings
        provider.db = fake_db
        fake_db._chroma_memory = provider  # type: ignore[attr-defined]
        provider.backup_service = None
        provider.data_dir = target_dir
        provider.data_dir.mkdir(parents=True, exist_ok=True)
        provider.client = None
        provider._chroma_client = None
        provider._collection = None
        from app.services.chroma_memory_provider import LightweightEmbeddingFunction

        provider._embedding_function = LightweightEmbeddingFunction()
        provider._migrated_from_supabase = False
        provider._init_chroma()
        return provider

    _ = orig_init
    mem_1 = _make_provider(dir_1)
    monkeypatch.setattr(
        mem_1,
        "_extract_memories",
        AsyncMock(
            return_value={
                "facts": [
                    {"fact": "Дмитрий коллекционирует виниловые пластинки Pink Floyd", "confidence": 0.94, "entities": ["Дмитрий"]}
                ]
            }
        ),
    )
    await mem_1.save_transcript(chat_id, "Дмитрий любит винил", ["Дмитрий"])
    mem_1.add_single_fact(chat_id, "В чате каждую пятницу проходит вечер настолок", "")
    mem_1.add_chat_event(chat_id, "Дмитрий сорвал джекпот 5000 печенек!", "Дмитрий")
    mem_1.store_single_fact(chat_id, "Временный факт для удаления", "Дмитрий")

    # Delete the temporary fact before restart -> must NOT resurrect on cold start!
    paged_1 = mem_1.get_all_facts_paged(chat_id, page=1, page_size=10)
    temp_fact = next(f for f in paged_1["facts"] if "Временный факт" in f["text"])
    assert mem_1.delete_fact_by_id(chat_id, temp_fact["id"]) is True

    # Simulate Render ephemeral disk wipe: brand-new empty directory `dir_2`, same SupabaseDB!
    dir_2 = tmp_path / "chroma_instance_2_fresh_after_render_restart"
    mem_2 = _make_provider(dir_2)
    paged_2 = mem_2.get_all_facts_paged(chat_id, page=1, page_size=10)

    restored_texts = [f["text"] for f in paged_2["facts"]]
    assert len(restored_texts) == 3
    assert any("Pink Floyd" in t for t in restored_texts)
    assert any("вечер настолок" in t for t in restored_texts)
    assert any("джекпот 5000" in t for t in restored_texts)
    assert not any("Временный факт" in t for t in restored_texts)


@pytest.mark.asyncio
async def test_newest_chroma_facts_prioritized_over_old_db_profile_notes(
    chroma_memory: ChromaMemoryProvider,
    fake_db: SupabaseDB,
) -> None:
    chat_id = -100888444
    sender = Sender(user_id=808, first_name="Игорь", username="igor_dev")
    user = fake_db.get_or_create_user(chat_id, sender)
    fake_db.update_user(
        user.id,
        {
            "bio": "Старое био 2023 года",
            "ai_notes": "Старая заметка 1\nСтарая заметка 2\nСтарая заметка 3\nСтарая заметка 4\nСтарая заметка 5",
        },
    )

    chroma_memory.store_single_fact(chat_id, "Игорь купил мотоцикл Yamaha в 2026 году", "Игорь", user_id=808)
    context = await chroma_memory.get_relevant_facts(chat_id, "Что нового у меня?", "Игорь", user_id=808)
    assert "Yamaha" in context, "Newly stored ChromaDB fact must not be hidden behind old DB bio/ai_notes"


# ==============================================================================
# 3. Render Anti-Sleep Endpoints & URL Validation
# ==============================================================================


def test_render_ping_and_healthz_endpoints_and_workflow(
    monkeypatch: pytest.MonkeyPatch,
    fake_db: SupabaseDB,
    test_settings: Settings,
    chroma_memory: ChromaMemoryProvider,
    mock_bot: MagicMock,
) -> None:
    assert web_mod.is_public_keepalive_url("http://127.0.0.1:8080") is False
    assert web_mod.is_public_keepalive_url("https://your-service.onrender.com") is False
    assert web_mod.is_public_keepalive_url("https://neuronika-bot.onrender.com") is True

    monkeypatch.setattr(web_mod, "get_settings", lambda: test_settings.model_copy(update={"keep_alive_enabled": False}))
    monkeypatch.setattr(web_mod, "Bot", lambda token: mock_bot)
    monkeypatch.setattr(web_mod, "SupabaseDB", lambda settings: fake_db)
    monkeypatch.setattr(web_mod, "build_memory_provider", lambda settings, db, backup=None: chroma_memory)
    mock_bot.set_webhook = AsyncMock(return_value=True)
    mock_bot.set_my_commands = AsyncMock(return_value=True)
    mock_bot.session = SimpleNamespace(close=AsyncMock())

    app = web_mod.create_app()
    with TestClient(app) as client:
        r_ping = client.get("/ping")
        assert r_ping.status_code == 200
        assert r_ping.json()["ok"] is True
        assert r_ping.json()["status"] == "awake"

        r_head = client.head("/ping")
        assert r_head.status_code == 200

        r_healthz = client.get("/healthz")
        assert r_healthz.status_code == 200
        assert r_healthz.json()["ok"] is True

    workflow_file = REPO_ROOT / ".github" / "workflows" / "keep_render_awake.yml"
    assert workflow_file.exists()
    wf_text = workflow_file.read_text(encoding="utf-8")
    assert "*/10 * * * *" in wf_text
    assert "/ping" in wf_text


# ==============================================================================
# 4. Captcha / Verification Surgical Improvements
# ==============================================================================


def test_captcha_image_legible_dimensions_and_contrast() -> None:
    buf = build_captcha_image("4829")
    img = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    assert img.size == (260, 110)
    raw = img.tobytes()
    dark_pixels = sum(
        1
        for idx in range(0, len(raw), 3)
        if raw[idx] < 100 and raw[idx + 1] < 100 and raw[idx + 2] < 140
    )
    assert dark_pixels > 250, f"Expected large legible digit strokes, got only {dark_pixels} dark pixels"


@pytest.mark.asyncio
async def test_captcha_superadmin_skip_and_five_wrong_attempts_kick(
    monkeypatch: pytest.MonkeyPatch,
    fake_db: SupabaseDB,
    test_settings: Settings,
    chroma_memory: ChromaMemoryProvider,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    monkeypatch.setattr(web_mod, "get_settings", lambda: test_settings)
    monkeypatch.setattr(web_mod, "Bot", lambda token: mock_bot)
    monkeypatch.setattr(web_mod, "SupabaseDB", lambda settings: fake_db)
    monkeypatch.setattr(web_mod, "build_memory_provider", lambda settings, db, backup=None: chroma_memory)

    app = web_mod.create_app()
    dp: Dispatcher = app.state.dispatcher
    chat_id = -100999111

    # 1. Superadmin joins group -> NOT restricted, NO pending captcha created
    dev_id = 861713427
    join_dev = message_factory(
        "",
        chat_id=chat_id,
        user_id=dev_id,
        new_chat_members=[SimpleNamespace(id=dev_id, full_name="Creator", username="dev", is_bot=False)],
        message_id=7001,
    )
    await dp.propagate_event(update_type="message", event=join_dev, bot=mock_bot)
    assert fake_db.get_verification(dev_id, chat_id=chat_id) is None

    # 2. Spammer joins group -> captcha issued; 5 wrong guesses -> kicked + unbanned + prompt deleted
    spammer_id = 666777
    mock_bot.ban_chat_member.reset_mock()
    mock_bot.unban_chat_member = AsyncMock(return_value=True)
    mock_bot.delete_message.reset_mock()

    join_spammer = message_factory(
        "",
        chat_id=chat_id,
        user_id=spammer_id,
        new_chat_members=[SimpleNamespace(id=spammer_id, full_name="BotSpam", username="botspam", is_bot=False)],
        message_id=7002,
    )
    await dp.propagate_event(update_type="message", event=join_spammer, bot=mock_bot)
    chal = fake_db.get_verification(spammer_id, chat_id=chat_id)
    assert chal is not None
    chal.code = "7391"

    for attempt_idx in range(5):
        wrong_msg = message_factory(
            f"000{attempt_idx}",
            chat_id=chat_id,
            user_id=spammer_id,
            message_id=7010 + attempt_idx,
        )
        await dp.propagate_event(update_type="message", event=wrong_msg, bot=mock_bot)
        assert wrong_msg.delete.called
        if attempt_idx < 4:
            assert fake_db.get_verification(spammer_id, chat_id=chat_id) is not None

    assert fake_db.get_verification(spammer_id, chat_id=chat_id) is None
    assert mock_bot.ban_chat_member.called
    assert mock_bot.unban_chat_member.called
    assert mock_bot.delete_message.called


@pytest.mark.asyncio
async def test_captcha_timeout_unbans_and_deletes_prompt_even_if_ban_fails(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    import app.bot.messages as messages_mod
    from app.bot.messages import build_messages_router

    chat_id = -100999222
    user_id = 777888
    mock_bot.ban_chat_member = AsyncMock(side_effect=RuntimeError("not enough rights"))
    mock_bot.delete_message = AsyncMock(return_value=True)
    mock_bot.restrict_chat_member = AsyncMock(return_value=True)
    mock_bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=8899))

    orig_vc = messages_mod.VerificationChallenge

    def _fast_vc(**kwargs: Any) -> VerificationChallenge:
        kwargs["timeout_seconds"] = 0
        return orig_vc(**kwargs)

    messages_mod.VerificationChallenge = _fast_vc  # type: ignore[misc]
    try:
        dp = Dispatcher()
        dp.include_router(build_messages_router(mock_bot, test_settings, fake_db, ai_service))
        join_evt = message_factory(
            "",
            chat_id=chat_id,
            user_id=user_id,
            username="late",
            new_chat_members=[SimpleNamespace(id=user_id, full_name="LateUser", username="late", is_bot=False)],
            message_id=8800,
        )
        await dp.propagate_event(update_type="message", event=join_evt, bot=mock_bot)
        await asyncio.sleep(0.05)
        assert fake_db.get_verification(user_id, chat_id=chat_id) is None
        mock_bot.delete_message.assert_called_with(chat_id, 8899)
    finally:
        messages_mod.VerificationChallenge = orig_vc  # type: ignore[misc]
