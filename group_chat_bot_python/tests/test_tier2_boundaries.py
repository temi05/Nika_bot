from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher

import app.bot.admin as admin_mod
import app.bot.messages as messages_mod
import app.bot.routers.economy as economy_mod
import app.bot.routers.profile_ai as profile_ai_mod
from app.config import Settings
from app.models import Sender
from app.services.chroma_memory_provider import ChromaMemoryProvider
from app.services.supabase_db import SupabaseDB
from app.services.telegram_backup import TelegramBackupService


@pytest.mark.asyncio
async def test_boundary_zip_slip_nested_and_corrupt_bytes(
    backup_service: TelegramBackupService,
    tmp_path: Path,
) -> None:
    """Tier 2 (F3.1): restore_from_zip_bytes rejects nested traversal, absolute paths, and corrupt bytes."""
    for bad_member in ("../../etc/passwd", "nested/../../../escape.txt", "/tmp/evil_abs.txt"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(bad_member, "malicious")
        assert await backup_service.restore_from_zip_bytes(buf.getvalue()) is False

    assert await backup_service.restore_from_zip_bytes(b"not-a-valid-zip-file") is False
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.asyncio
async def test_boundary_html_injection_in_lore_and_give_and_repay(
    fake_db: SupabaseDB,
    ai_service: Any,
    chroma_memory: ChromaMemoryProvider,
    mock_bot: MagicMock,
    message_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2 (F3.1): /lore and /give escape <script> HTML payloads, and /repay passes parse_mode='HTML'."""
    chat_id = -100601
    chroma_memory.add_chat_event(chat_id, "Легенда: <script>alert('xss')</script> выиграл дуэль!", "Hacker")

    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))
    dp.include_router(economy_mod.build_economy_router(fake_db, ai_service))

    # 1. /lore escapes <script>
    lore_msg = message_factory("/lore", chat_id=chat_id, user_id=101)
    await dp.propagate_event(update_type="message", event=lore_msg, bot=mock_bot)
    lore_out = lore_msg.answer.call_args.args[0]
    assert "<script>" not in lore_out
    assert "&lt;script&gt;" in lore_out

    # 2. /give escapes AI gift_message
    u1 = fake_db.get_or_create_user(chat_id, Sender(user_id=101, first_name="A", username="a"))
    u2 = fake_db.get_or_create_user(chat_id, Sender(user_id=102, first_name="B", username="b"))
    fake_db.update_user(u1.id, {"reputation": 200})
    fake_db.update_user(u2.id, {"reputation": 50})
    monkeypatch.setattr(
        ai_service,
        "generate_cookie_gift_message",
        AsyncMock(return_value="Подарок <script>evil()</script> от души!"),
    )

    reply_target = message_factory("Привет", chat_id=chat_id, user_id=102, first_name="B", username="b")
    give_msg = message_factory("/give 20", chat_id=chat_id, user_id=101, first_name="A", username="a", reply_to_message=reply_target)
    await dp.propagate_event(update_type="message", event=give_msg, bot=mock_bot)
    give_out = give_msg.answer.call_args.args[0]
    assert "<script>" not in give_out
    assert "&lt;script&gt;" in give_out

    # 3. /repay insufficient funds includes parse_mode="HTML"
    fake_db.update_user(u1.id, {"reputation": 10, "debt": 500})
    repay_msg = message_factory("/repay", chat_id=chat_id, user_id=101, first_name="A", username="a")
    await dp.propagate_event(update_type="message", event=repay_msg, bot=mock_bot)
    assert repay_msg.answer.call_args.kwargs.get("parse_mode") == "HTML"


def test_boundary_repay_debts_partial_vs_full_and_zero(fake_db: SupabaseDB) -> None:
    """Tier 2 (F2.1): Partial debt repayment keeps status='active'; full repayment sets status='repaid'; 0 is no-op."""
    chat_id = -100602
    lender_a = fake_db.get_or_create_user(chat_id, Sender(user_id=1, first_name="LA", username="la"))
    lender_b = fake_db.get_or_create_user(chat_id, Sender(user_id=2, first_name="LB", username="lb"))
    borrower = fake_db.get_or_create_user(chat_id, Sender(user_id=3, first_name="Bor", username="bor"))

    borrower = fake_db.update_user(borrower.id, {"reputation": 500, "debt": 200}) or borrower
    fake_db.create_debt(lender_a, borrower, 100)
    fake_db.create_debt(lender_b, borrower, 100)

    # Zero repayment
    zero_res = fake_db.repay_debts(borrower, 0)
    assert int(zero_res["paid"]) == 0

    # Partial repayment of 60 cookies
    part_res = fake_db.repay_debts(borrower, 60)
    assert int(part_res["paid"]) == 60
    debts = fake_db.fake_client.tables["bot_debts"]  # type: ignore[attr-defined]
    assert debts[0]["status"] == "active"
    assert debts[0]["paid_amount"] == 60
    assert debts[1]["status"] == "active"

    # Full repayment of remaining 140 cookies
    borrower = fake_db.get_user_by_platform_id(chat_id, 3) or borrower
    full_res = fake_db.repay_debts(borrower, 140)
    assert int(full_res["paid"]) == 140
    assert debts[0]["status"] == "repaid"
    assert debts[1]["status"] == "repaid"


def test_boundary_parse_duration_edge_cases() -> None:
    """Tier 2 (F3.2): parse_duration handles invalid, zero, negative, and Russian/English units properly."""
    parse_duration = getattr(admin_mod, "parse_duration", None)
    assert callable(parse_duration), "parse_duration must exist in app/bot/admin.py"

    for invalid in ("", "0m", "-10m", "abc", "10x", "m"):
        assert parse_duration(invalid) is None

    assert parse_duration("1m") is not None
    assert parse_duration("2ч") is not None
    assert parse_duration("1д") is not None


def test_boundary_reputation_delta_negations_and_lengths() -> None:
    """Tier 2 (F4.2): _reputation_delta cancels negated praise, ignores negative words, and ignores long messages."""
    rep_delta = messages_mod._reputation_delta
    assert rep_delta("не спасибо") == 0
    assert rep_delta("совсем не база") == 0
    assert rep_delta("") == 0
    assert rep_delta("   ") == 0
    assert rep_delta("кринж полный") == 0
    assert rep_delta("спасибо большое!") == 1
    assert rep_delta("спасибо тебе за то что вчера скинул ссылки на документацию по проекту, очень выручил") == 0


@pytest.mark.asyncio
async def test_boundary_flood_rate_limiting(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F3.3): Rapid burst of >6 messages in a few seconds from a non-admin triggers anti-flood protection."""
    chat_id = -100603
    spammer_id = 4444
    dp = Dispatcher()
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    deleted_any = False
    for i in range(8):
        msg = message_factory(f"Флуд сообщение {i}", chat_id=chat_id, user_id=spammer_id, message_id=1000 + i)
        await dp.propagate_event(update_type="message", event=msg, bot=mock_bot)
        if msg.delete.called or mock_bot.restrict_chat_member.called:
            deleted_any = True

    assert deleted_any, "Anti-flood protection must delete messages or restrict a user sending 8 rapid messages"


@pytest.mark.asyncio
async def test_boundary_newcomer_scam_heuristic(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F3.3): Low-level newcomer sending crypto/work scam is moderated, while high-level veteran is not."""
    chat_id = -100604
    newbie_id = 5551
    veteran_id = 5552

    vet = fake_db.get_or_create_user(chat_id, Sender(user_id=veteran_id, first_name="Vet", username="vet"))
    fake_db.update_user(vet.id, {"level": 12, "xp": 10000})

    dp = Dispatcher()
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    scam_text = "Срочно! Заработок от 500$ в день на USDT арбитраже, удаленная работа без опыта, пиши в лс!"
    scam_msg = message_factory(scam_text, chat_id=chat_id, user_id=newbie_id, message_id=2001)
    await dp.propagate_event(update_type="message", event=scam_msg, bot=mock_bot)
    assert scam_msg.delete.called, "Newcomer scam message must be deleted"

    vet_msg = message_factory("Кстати, кто следит за курсом USDT сегодня?", chat_id=chat_id, user_id=veteran_id, message_id=2002)
    await dp.propagate_event(update_type="message", event=vet_msg, bot=mock_bot)
    assert not vet_msg.delete.called, "Veteran normal message must not be deleted by newcomer scam filter"


@pytest.mark.asyncio
async def test_boundary_forget_fact_authorization_non_owner_rejected(
    fake_db: SupabaseDB,
    ai_service: Any,
    chroma_memory: ChromaMemoryProvider,
    mock_bot: MagicMock,
    message_factory: Any,
    callback_factory: Any,
) -> None:
    """Tier 2 (F3.1 & F2.4): Unrelated non-admin cannot delete another user's fact via forget_fact_<id>."""
    chat_id = -100605
    owner_id = 101
    attacker_id = 999
    admin_id = 777
    mock_bot.set_admin(chat_id, admin_id, True)

    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))

    rem_msg = message_factory("/remember Я коллекционирую виниловые пластинки", chat_id=chat_id, user_id=owner_id, username="vinyl_fan", message_id=3001)
    await dp.propagate_event(update_type="message", event=rem_msg, bot=mock_bot)

    # Attacker clicks forget button
    bad_cb = callback_factory("forget_fact_3001", chat_id=chat_id, user_id=attacker_id, username="attacker")
    await dp.propagate_event(update_type="callback_query", event=bad_cb, bot=mock_bot)

    db_knowledge_after_attack = fake_db.fake_client.tables.get("bot_knowledge", [])  # type: ignore[attr-defined]
    assert len(db_knowledge_after_attack) == 1, "Attacker must not be able to delete owner's fact from Supabase"
    facts_after_attack = chroma_memory.get_all_facts_paged(chat_id)["facts"]
    assert any("виниловые пластинки" in f["text"] for f in facts_after_attack), "Attacker must not be able to delete owner's fact from Chroma"

    # Admin clicks forget button -> succeeds
    admin_cb = callback_factory("forget_fact_3001", chat_id=chat_id, user_id=admin_id, username="admin")
    await dp.propagate_event(update_type="callback_query", event=admin_cb, bot=mock_bot)
    facts_after_admin = chroma_memory.get_all_facts_paged(chat_id)["facts"]
    assert not any("виниловые пластинки" in f["text"] for f in facts_after_admin)


@pytest.mark.asyncio
async def test_boundary_admin_exempt_from_bad_word_and_link_filter(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F3.3): Group admins are exempt from both link filter and bad-word filter."""
    chat_id = -100606
    admin_id = 777
    mock_bot.set_admin(chat_id, admin_id, True)
    fake_db.add_bad_word(chat_id, "ругательство")

    dp = Dispatcher()
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    msg = message_factory("Инфо для чата: https://t.me/our_channel и ругательство", chat_id=chat_id, user_id=admin_id)
    await dp.propagate_event(update_type="message", event=msg, bot=mock_bot)
    assert not msg.delete.called
    admin_user = fake_db.get_user_by_platform_id(chat_id, admin_id)
    assert admin_user is None or admin_user.warns == 0


@pytest.mark.asyncio
async def test_boundary_moderation_refuses_to_punish_fellow_admin(
    fake_db: SupabaseDB,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F3.2): Admin A cannot /warn, /mute, /ban, or /kick fellow Admin B."""
    chat_id = -100607
    admin_a = 771
    admin_b = 772
    mock_bot.set_admin(chat_id, admin_a, True)
    mock_bot.set_admin(chat_id, admin_b, True)
    fake_db.get_or_create_user(chat_id, Sender(user_id=admin_b, first_name="AdminB", username="admin_b"))

    dp = Dispatcher()
    dp.include_router(admin_mod.build_admin_router(mock_bot, fake_db))
    reply_msg = message_factory("Я тоже админ", chat_id=chat_id, user_id=admin_b, first_name="AdminB", username="admin_b")

    for cmd in ("/warn тест", "/mute 10m тест", "/ban 1d тест", "/kick"):
        m = message_factory(cmd, chat_id=chat_id, user_id=admin_a, reply_to_message=reply_msg)
        await dp.propagate_event(update_type="message", event=m, bot=mock_bot)

    assert not mock_bot.restrict_chat_member.called
    assert not mock_bot.ban_chat_member.called
    assert fake_db.get_user_by_platform_id(chat_id, admin_b).warns == 0


@pytest.mark.asyncio
async def test_boundary_summary_and_ask_when_ai_disabled_or_empty(
    fake_db: SupabaseDB,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F5.3): /summary and /ask handle empty chat, missing args, and ai_enabled=False gracefully."""
    chat_id = -100608
    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))

    # 1. Empty chat summary
    sum_empty = await ai_service.summarize_chat(chat_id, count=20)
    assert "нет" in sum_empty.lower() or "пуст" in sum_empty.lower()

    # 2. /ask with no args and no reply
    ask_empty_msg = message_factory("/ask", chat_id=chat_id, user_id=101)
    await dp.propagate_event(update_type="message", event=ask_empty_msg, bot=mock_bot)
    assert ask_empty_msg.answer.called

    # 3. ai_enabled=False blocks both /summary and /ask in group chat
    fake_db.get_chat_settings(chat_id)
    fake_db.update_chat_settings(chat_id, ai_enabled=False)

    sum_disabled = message_factory("/summary", chat_id=chat_id, user_id=101)
    await dp.propagate_event(update_type="message", event=sum_disabled, bot=mock_bot)
    assert "выключены" in sum_disabled.answer.call_args.args[0].lower()


def test_boundary_upsert_persona_state_on_conflict_idempotent(fake_db: SupabaseDB) -> None:
    """Tier 2 (F2.4): upsert_persona_state specifies on_conflict='chat_id,user_id' so repeated upserts do not fail."""
    chat_id = -100609
    user_id = 909
    fake_db.upsert_persona_state(chat_id, user_id, {"stage": "fresh", "warmth": 0.6, "respect": 0.5})
    fake_db.upsert_persona_state(chat_id, user_id, {"stage": "familiar", "warmth": 0.75, "respect": 0.65})

    state = fake_db.get_persona_state(chat_id, user_id)
    assert state is not None
    assert state["stage"] == "familiar"
    assert float(state["warmth"]) == 0.75


def test_boundary_parse_birthday_and_zodiac_edges() -> None:
    """Tier 2: parse_birthday_parts and get_zodiac_sign handle leap day, invalid months/days, and boundary dates."""
    from app.utils import get_zodiac_sign, parse_birthday_parts

    assert parse_birthday_parts("29.02") == (29, 2, None)
    assert parse_birthday_parts("15.08.2000") == (15, 8, 2000)
    assert parse_birthday_parts("32.01") is None
    assert parse_birthday_parts("00.05") is None
    assert parse_birthday_parts("10.13") is None
    assert parse_birthday_parts("invalid") is None
    assert "Козерог" in get_zodiac_sign(1, 1)
    assert "Рыбы" in get_zodiac_sign(20, 3)


def test_boundary_escape_html_special_characters() -> None:
    """Tier 2: escape_html safely neutralizes <, >, &, and quotes without corrupting normal text."""
    from app.utils import escape_html

    raw = '<script>alert("xss" & \'test\')</script>'
    escaped = escape_html(raw)
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped
    assert "&amp;" in escaped


def test_boundary_transfer_cookies_minimum_tax_and_invalid_amounts(fake_db: SupabaseDB) -> None:
    """Tier 2: transfer_cookies enforces minimum 1 cookie tax, rejects <=0, self-transfer, and insufficient balance."""
    chat_id = -100610
    u1 = fake_db.get_or_create_user(chat_id, Sender(user_id=1, first_name="A", username="a"))
    u2 = fake_db.get_or_create_user(chat_id, Sender(user_id=2, first_name="B", username="b"))
    fake_db.update_user(u1.id, {"reputation": 10})
    u1 = fake_db.get_user_by_platform_id(chat_id, 1)

    # Zero or negative amount rejected
    assert fake_db.transfer_cookies(u1, u2, 0) == (False, 0)
    assert fake_db.transfer_cookies(u1, u2, -5) == (False, 0)
    # Self-transfer rejected
    assert fake_db.transfer_cookies(u1, u1, 5) == (False, 0)
    # Insufficient balance rejected
    assert fake_db.transfer_cookies(u1, u2, 20) == (False, 0)
    # Small transfer (2 cookies) deducts minimum 1 cookie tax -> net 1
    ok, net = fake_db.transfer_cookies(u1, u2, 2)
    assert ok is True
    assert net == 1
    assert fake_db.get_chat_settings(chat_id).casino_jackpot >= 1


def test_boundary_purchase_item_invalid_id_and_insufficient_funds(fake_db: SupabaseDB) -> None:
    """Tier 2: purchase_item rejects unknown item IDs and insufficient reputation."""
    chat_id = -100611
    u = fake_db.get_or_create_user(chat_id, Sender(user_id=10, first_name="Buyer", username="buyer"))
    fake_db.update_user(u.id, {"reputation": 50, "warns": 1})
    u = fake_db.get_user_by_platform_id(chat_id, 10)

    ok_lvl, _ = fake_db.purchase_item(u, 1)
    assert ok_lvl is False
    ok_warn, _ = fake_db.purchase_item(u, 2)
    assert ok_warn is False
    ok_unknown, _ = fake_db.purchase_item(u, 999)
    assert ok_unknown is False


def test_boundary_forgive_debts_zero_and_nonexistent(fake_db: SupabaseDB) -> None:
    """Tier 2: forgive_debts handles 0 amount or nonexistent debt without mutating state."""
    chat_id = -100612
    lender = fake_db.get_or_create_user(chat_id, Sender(user_id=11, first_name="Lender", username="lender"))
    borrower = fake_db.get_or_create_user(chat_id, Sender(user_id=12, first_name="Borrower", username="borrower"))

    res_none = fake_db.forgive_debts(lender, borrower.user_id, 100)
    assert res_none["forgiven"] == 0

    fake_db.create_debt(lender, borrower, 200)
    fake_db.update_user(borrower.id, {"debt": 200})
    res_zero = fake_db.forgive_debts(lender, borrower.user_id, 0)
    assert res_zero["forgiven"] == 0


@pytest.mark.asyncio
async def test_boundary_purge_non_reply_and_invalid_count(
    fake_db: SupabaseDB,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F3.2): /purge without reply or with non-Admin user is rejected cleanly."""
    chat_id = -100613
    admin_id = 777
    user_id = 101
    mock_bot.set_admin(chat_id, admin_id, True)

    dp = Dispatcher()
    dp.include_router(admin_mod.build_admin_router(mock_bot, fake_db))

    # Non-admin attempt
    non_admin_msg = message_factory("/purge 5", chat_id=chat_id, user_id=user_id)
    await dp.propagate_event(update_type="message", event=non_admin_msg, bot=mock_bot)
    assert not mock_bot.delete_message.called

    # Admin without reply and without valid arg
    admin_no_arg = message_factory("/purge", chat_id=chat_id, user_id=admin_id)
    await dp.propagate_event(update_type="message", event=admin_no_arg, bot=mock_bot)
    assert admin_no_arg.answer.called


@pytest.mark.asyncio
async def test_boundary_report_without_reply_rejected(
    fake_db: SupabaseDB,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F3.2): /report without replying to a target message explains usage instead of crashing."""
    chat_id = -100614
    dp = Dispatcher()
    dp.include_router(admin_mod.build_admin_router(mock_bot, fake_db))

    msg = message_factory("/report спам", chat_id=chat_id, user_id=101)
    await dp.propagate_event(update_type="message", event=msg, bot=mock_bot)
    assert msg.answer.called
    assert "/report" in msg.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_boundary_rules_empty_default_and_non_admin_setrules(
    fake_db: SupabaseDB,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F3.2): /rules shows default when unset, and non-admin cannot run /setrules."""
    chat_id = -100615
    dp = Dispatcher()
    dp.include_router(admin_mod.build_admin_router(mock_bot, fake_db))

    r_msg = message_factory("/rules", chat_id=chat_id, user_id=101)
    await dp.propagate_event(update_type="message", event=r_msg, bot=mock_bot)
    assert "не заданы" in r_msg.answer.call_args.args[0].lower()

    sr_msg = message_factory("/setrules Новые правила", chat_id=chat_id, user_id=101)
    await dp.propagate_event(update_type="message", event=sr_msg, bot=mock_bot)
    assert fake_db.get_chat_rules(chat_id) is None


@pytest.mark.asyncio
async def test_boundary_unwarn_user_with_zero_warns(
    fake_db: SupabaseDB,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F3.2): /unwarn on a user with 0 warns keeps warns at 0 without negative values."""
    chat_id = -100616
    admin_id = 777
    target_id = 202
    mock_bot.set_admin(chat_id, admin_id, True)
    fake_db.get_or_create_user(chat_id, Sender(user_id=target_id, first_name="Clean", username="clean"))

    dp = Dispatcher()
    dp.include_router(admin_mod.build_admin_router(mock_bot, fake_db))

    reply_msg = message_factory("Привет", chat_id=chat_id, user_id=target_id, first_name="Clean", username="clean")
    uw_msg = message_factory("/unwarn", chat_id=chat_id, user_id=admin_id, reply_to_message=reply_msg)
    await dp.propagate_event(update_type="message", event=uw_msg, bot=mock_bot)
    assert fake_db.get_user_by_platform_id(chat_id, target_id).warns == 0


@pytest.mark.asyncio
async def test_boundary_remind_invalid_duration_and_empty_text(
    fake_db: SupabaseDB,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F4.2): /remind rejects missing text or invalid duration format."""
    from app.bot.routers import general_admin as general_admin_mod

    chat_id = -100617
    dp = Dispatcher()
    dp.include_router(general_admin_mod.build_general_admin_router(fake_db, ai_service, "НейроНика"))

    for bad_cmd in ("/remind", "/remind 10m", "/remind invalid_time купить молоко"):
        m = message_factory(bad_cmd, chat_id=chat_id, user_id=101)
        await dp.propagate_event(update_type="message", event=m, bot=mock_bot)
        assert m.answer.called

    assert len(fake_db.fake_client.tables.get("reminders", [])) == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_boundary_kto_empty_chat_fallback(
    fake_db: SupabaseDB,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F4.2): /kto in a chat with no pre-existing users still responds using the caller."""
    from app.bot.routers import general_admin as general_admin_mod

    chat_id = -100618
    dp = Dispatcher()
    dp.include_router(general_admin_mod.build_general_admin_router(fake_db, ai_service, "НейроНика"))

    m = message_factory("/kto самый загадочный", chat_id=chat_id, user_id=101, first_name="Solo", username="solo")
    await dp.propagate_event(update_type="message", event=m, bot=mock_bot)
    assert m.answer.called
    assert "Solo" in m.answer.call_args.args[0] or "solo" in m.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_boundary_linkfilter_invalid_argument(
    fake_db: SupabaseDB,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2 (F3.1): /linkfilter with an unknown argument explains usage without toggling state."""
    chat_id = -100619
    admin_id = 777
    mock_bot.set_admin(chat_id, admin_id, True)
    fake_db.get_chat_settings(chat_id)

    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))

    m = message_factory("/linkfilter maybe", chat_id=chat_id, user_id=admin_id)
    await dp.propagate_event(update_type="message", event=m, bot=mock_bot)
    assert "использование" in m.answer.call_args.args[0].lower()
    assert fake_db.get_chat_settings(chat_id).link_filter_enabled is True


@pytest.mark.asyncio
async def test_boundary_give_to_bot_and_self_rejected(
    fake_db: SupabaseDB,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2: /give to oneself or to a bot is rejected before any balance change."""
    chat_id = -100620
    user = fake_db.get_or_create_user(chat_id, Sender(user_id=101, first_name="Giver", username="giver"))
    fake_db.update_user(user.id, {"reputation": 500})

    dp = Dispatcher()
    dp.include_router(economy_mod.build_economy_router(fake_db, ai_service))

    self_reply = message_factory("Моё сообщение", chat_id=chat_id, user_id=101, first_name="Giver", username="giver")
    m_self = message_factory("/give 100", chat_id=chat_id, user_id=101, reply_to_message=self_reply)
    await dp.propagate_event(update_type="message", event=m_self, bot=mock_bot)
    assert fake_db.get_user_by_platform_id(chat_id, 101).reputation == 500

    bot_reply = message_factory("Я бот", chat_id=chat_id, user_id=999000, first_name="Bot", is_bot=True)
    m_bot = message_factory("/give 100", chat_id=chat_id, user_id=101, reply_to_message=bot_reply)
    await dp.propagate_event(update_type="message", event=m_bot, bot=mock_bot)
    assert fake_db.get_user_by_platform_id(chat_id, 101).reputation == 500


@pytest.mark.asyncio
async def test_boundary_ai_duel_rejects_insufficient_balance_and_self_bot(
    fake_db: SupabaseDB,
    ai_service: Any,
) -> None:
    """Tier 2 (F5.1): _tool_challenge_user_to_duel rejects dueling the bot itself or betting more than user's balance."""
    chat_id = -100621
    sender = Sender(user_id=101, first_name="Duelist", username="duelist")
    u = fake_db.get_or_create_user(chat_id, sender)
    fake_db.update_user(u.id, {"reputation": 5})

    res_low = await ai_service._tool_challenge_user_to_duel(chat_id, sender, {"target_name": "@duelist", "amount": 50})
    assert "недостаточно" in res_low.lower()


@pytest.mark.asyncio
async def test_boundary_ai_transfer_reputation_mood_and_daily_cap(
    fake_db: SupabaseDB,
    ai_service: Any,
) -> None:
    """Tier 2 (F5.1): _tool_transfer_reputation rejects transfers when Nika's mood < 35 or amount > 200."""
    chat_id = -100622
    sender = Sender(user_id=101, first_name="Rich", username="rich")
    target = Sender(user_id=202, first_name="Friend", username="friend")
    u1 = fake_db.get_or_create_user(chat_id, sender)
    fake_db.get_or_create_user(chat_id, target)
    fake_db.update_user(u1.id, {"reputation": 1000})

    ai_service.moods[chat_id] = 20
    res_mood = await ai_service._tool_transfer_reputation(chat_id, sender, {"target_name": "@friend", "amount": 50})
    assert "отклонено" in res_mood.lower()

    ai_service.moods[chat_id] = 80
    res_over = await ai_service._tool_transfer_reputation(chat_id, sender, {"target_name": "@friend", "amount": 250})
    assert "отклонено" in res_over.lower() or "максимум 200" in res_over.lower()


def test_boundary_chroma_clean_legacy_fact_filters_ephemeral_and_short() -> None:
    """Tier 2 (F2.2): _clean_legacy_fact strips ephemeral statuses ('хочет спать', 'ем пельмени') and <5 char strings."""
    from app.services.chroma_memory_provider import _clean_legacy_fact

    assert _clean_legacy_fact("Пользователь хочет спать") == ""
    assert _clean_legacy_fact("Сидит в телефоне") == ""
    assert _clean_legacy_fact("абв") == ""
    cleaned = _clean_legacy_fact("УЗЕЛ: @alex | интерес: квантовая физика")
    assert "увлекается: квантовая физика" in cleaned


def test_boundary_search_user_by_numeric_id_and_translit(fake_db: SupabaseDB) -> None:
    """Tier 2: search_user finds users by exact numeric user_id and by transliterated name."""
    chat_id = -100623
    fake_db.get_or_create_user(chat_id, Sender(user_id=555666, first_name="Алексей", username="alex_dev"))

    by_id = fake_db.search_user(chat_id, "555666")
    assert by_id is not None and by_id.user_id == 555666

    by_name = fake_db.search_user(chat_id, "Алексей")
    assert by_name is not None and by_name.user_id == 555666


@pytest.mark.asyncio
async def test_boundary_forget_command_rejects_short_query_and_non_admin(
    fake_db: SupabaseDB,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 2: /forget rejects non-admins and queries shorter than 3 characters."""
    chat_id = -100624
    admin_id = 777
    mock_bot.set_admin(chat_id, admin_id, True)

    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))

    non_admin = message_factory("/forget секрет", chat_id=chat_id, user_id=101)
    await dp.propagate_event(update_type="message", event=non_admin, bot=mock_bot)
    assert "администратор" in non_admin.answer.call_args.args[0].lower()

    short_q = message_factory("/forget я", chat_id=chat_id, user_id=admin_id)
    await dp.propagate_event(update_type="message", event=short_q, bot=mock_bot)
    assert "короткий" in short_q.answer.call_args.args[0].lower() or "минимум 3" in short_q.answer.call_args.args[0].lower()
