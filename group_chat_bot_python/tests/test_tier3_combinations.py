from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher

import app.bot.messages as messages_mod
import app.bot.routers.chat_settings as chat_settings_mod
import app.bot.routers.economy as economy_mod
import app.bot.routers.general_admin as general_admin_mod
import app.bot.routers.memes as memes_mod
import app.bot.routers.profile_ai as profile_ai_mod
from app.config import Settings
from app.models import Sender
from app.services.chroma_memory_provider import ChromaMemoryProvider
from app.services.supabase_db import SupabaseDB


@pytest.mark.asyncio
async def test_combo_overdue_debt_auto_jail_blocks_economy_and_ai_transfer_then_bail_restores(
    fake_db: SupabaseDB,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
    callback_factory: Any,
) -> None:
    """Tier 3: Overdue debt auto-jails borrower on economy/AI actions; paying bail credits lender and restores freedom."""
    chat_id = -100701
    lender = fake_db.get_or_create_user(chat_id, Sender(user_id=10, first_name="Lender", username="lender"))
    borrower = fake_db.get_or_create_user(chat_id, Sender(user_id=20, first_name="Borrower", username="borrower"))
    fake_db.update_user(lender.id, {"reputation": 500})
    fake_db.update_user(borrower.id, {"reputation": 1000, "debt": 100})

    debt_row = fake_db.create_debt(lender, borrower, 100)
    assert debt_row is not None
    # Make debt overdue by setting due_at in the past
    past_due = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    fake_db.fake_client.table("bot_debts").update({"due_at": past_due}).eq("id", debt_row["id"]).execute()  # type: ignore[attr-defined]

    dp = Dispatcher()
    dp.include_router(economy_mod.build_economy_router(fake_db, ai_service))
    dp.include_router(general_admin_mod.build_general_admin_router(fake_db, "НейроНика", ai_service))

    reply_lender = message_factory("Жду возврат", chat_id=chat_id, user_id=10, first_name="Lender", username="lender")

    # 1. Borrower tries /give -> auto-jailed for overdue debt and blocked!
    give_msg = message_factory("/give 50", chat_id=chat_id, user_id=20, first_name="Borrower", username="borrower", reply_to_message=reply_lender)
    await dp.propagate_event(update_type="message", event=give_msg, bot=mock_bot)
    assert "тюрьм" in give_msg.answer.call_args.args[0].lower()

    # 2. Borrower tries AI _tool_transfer_reputation -> also blocked by jail!
    tx_res = await ai_service._tool_transfer_reputation(
        chat_id, Sender(user_id=20, first_name="Borrower", username="borrower"), {"target_name": "lender", "amount": 50}
    )
    assert "Отклонено" in tx_res or "тюрьм" in tx_res.lower()

    # 3. Borrower runs /bail and confirms payment
    bail_msg = message_factory("/bail", chat_id=chat_id, user_id=20, first_name="Borrower", username="borrower")
    await dp.propagate_event(update_type="message", event=bail_msg, bot=mock_bot)
    assert bail_msg.answer.called

    bail_cb = callback_factory("bail_pay_20", chat_id=chat_id, user_id=20, first_name="Borrower", username="borrower")
    await dp.propagate_event(update_type="callback_query", event=bail_cb, bot=mock_bot)

    freed_borrower = fake_db.get_user_by_platform_id(chat_id, 20)
    assert freed_borrower is not None and freed_borrower.jailed_until is None
    updated_lender = fake_db.get_user_by_platform_id(chat_id, 10)
    assert updated_lender is not None and updated_lender.reputation > 500


@pytest.mark.asyncio
async def test_combo_linkfilter_command_syncs_with_chatsettings_panel_and_message_moderation(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
    callback_factory: Any,
) -> None:
    """Tier 3: /linkfilter and /chatsettings share unified cache and control live link moderation."""
    chat_id = -100702
    admin_id = 111
    user_id = 222
    mock_bot.set_admin(chat_id, admin_id, True)

    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))
    dp.include_router(chat_settings_mod.build_chat_settings_router(fake_db))
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    # 1. Admin disables link filter via /linkfilter off
    lf_off = message_factory("/linkfilter off", chat_id=chat_id, user_id=admin_id)
    await dp.propagate_event(update_type="message", event=lf_off, bot=mock_bot)

    # 2. Admin opens /chatsettings -> shows Фильтр ссылок: Выкл
    cs_msg = message_factory("/chatsettings", chat_id=chat_id, user_id=admin_id)
    await dp.propagate_event(update_type="message", event=cs_msg, bot=mock_bot)
    assert "Фильтр ссылок: <b>Выкл</b>" in cs_msg.answer.call_args.args[0]

    # 3. User posts t.me link while filter is off -> NOT deleted
    link_ok = message_factory("Смотрите https://t.me/open_link", chat_id=chat_id, user_id=user_id, message_id=4001)
    await dp.propagate_event(update_type="message", event=link_ok, bot=mock_bot)
    assert not link_ok.delete.called

    # 4. Admin toggles link filter back ON via callback button
    toggle_cb = callback_factory("cs_toggle_linkfilter", chat_id=chat_id, user_id=admin_id)
    await dp.propagate_event(update_type="callback_query", event=toggle_cb, bot=mock_bot)

    # 5. User posts t.me link now -> deleted and warned!
    link_blocked = message_factory("Смотрите https://t.me/blocked_link", chat_id=chat_id, user_id=user_id, message_id=4002)
    await dp.propagate_event(update_type="message", event=link_blocked, bot=mock_bot)
    assert link_blocked.delete.called


@pytest.mark.asyncio
async def test_combo_meme_add_and_bio_and_remember_all_retrieved_in_ai_reply_and_card(
    fake_db: SupabaseDB,
    ai_service: Any,
    chroma_memory: ChromaMemoryProvider,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 3: /memeadd, /bio, and /remember all sync to memory so /card and get_relevant_facts include them."""
    chat_id = -100703
    admin_id = 111
    user_id = 333
    mock_bot.set_admin(chat_id, admin_id, True)

    dp = Dispatcher()
    dp.include_router(memes_mod.build_memes_router(fake_db))
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))

    meme_msg = message_factory("/memeadd Пельмени — главная валюта ночного чата", chat_id=chat_id, user_id=admin_id, username="admin")
    await dp.propagate_event(update_type="message", event=meme_msg, bot=mock_bot)

    bio_msg = message_factory("/bio DevOps инженер в облаках", chat_id=chat_id, user_id=user_id, username="cloud_guy")
    await dp.propagate_event(update_type="message", event=bio_msg, bot=mock_bot)

    rem_msg = message_factory("/remember Собираю кастомные механические клавиатуры", chat_id=chat_id, user_id=user_id, username="cloud_guy")
    await dp.propagate_event(update_type="message", event=rem_msg, bot=mock_bot)

    card_msg = message_factory("/card", chat_id=chat_id, user_id=user_id, username="cloud_guy")
    await dp.propagate_event(update_type="message", event=card_msg, bot=mock_bot)
    card_text = card_msg.answer.call_args.args[0]
    assert "DevOps инженер" in card_text
    assert "механические клавиатуры" in card_text

    facts_ctx = await chroma_memory.get_relevant_facts(chat_id, "пельмени клавиатуры облаках", "@cloud_guy", user_id=user_id)
    assert "механические клавиатуры" in facts_ctx
    assert "Пельмени" in facts_ctx


@pytest.mark.asyncio
async def test_combo_memory_signal_triggers_non_destructive_flush_and_preserves_history_for_reply(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    chroma_memory: ChromaMemoryProvider,
    mock_bot: MagicMock,
    message_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3: A message with a memory signal flushes passive memory without wiping context needed for generate_reply."""
    chat_id = -100704
    monkeypatch.setattr(chroma_memory, "save_transcript", AsyncMock())

    captured_prompts: list[Any] = []

    async def _capture_completion(**kwargs: Any) -> Any:
        captured_prompts.append(kwargs.get("messages"))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Прага — отличный город!", tool_calls=[]), finish_reason="stop")]
        )

    ai_service._chat_completion = AsyncMock(side_effect=_capture_completion)

    dp = Dispatcher()
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    # Send prior context messages
    m1 = message_factory("Мы сегодня обсуждаем переезд в Европу", chat_id=chat_id, user_id=401, username="u1", message_id=5001)
    await dp.propagate_event(update_type="message", event=m1, bot=mock_bot)

    # Send message that both mentions Nika AND triggers memory_signal ("я живу в...")
    m2 = message_factory("НейроНика, я живу в Праге уже два года, как тебе этот город?", chat_id=chat_id, user_id=402, username="u2", message_id=5002)
    await dp.propagate_event(update_type="message", event=m2, bot=mock_bot)

    assert chroma_memory.save_transcript.called
    assert len(captured_prompts) == 1
    user_prompt_content = str(captured_prompts[0][-1]["content"])
    assert "переезд в Европу" in user_prompt_content, "Prior chat context must remain visible after flush_passive_memory"


@pytest.mark.asyncio
async def test_combo_ai_tool_remember_fact_attaches_forget_button_and_deletes_from_both_stores(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    chroma_memory: ChromaMemoryProvider,
    mock_bot: MagicMock,
    message_factory: Any,
    callback_factory: Any,
) -> None:
    """Tier 3: AI remember_user_fact tool attaches forget_fact_<msg_id> button, and clicking it deletes from DB + Chroma."""
    chat_id = -100705
    user_id = 505

    round_responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                id="call_rem_1",
                                function=SimpleNamespace(
                                    name="remember_user_fact",
                                    arguments='{"target_name": "@guitar_hero", "fact": "Играет на электрогитаре Gibson"}',
                                ),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Запомнила про твою гитару Gibson!", tool_calls=[]),
                    finish_reason="stop",
                )
            ]
        ),
    ]
    ai_service._chat_completion = AsyncMock(side_effect=round_responses)

    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    msg = message_factory(
        "НейроНика, я вчера купил электрогитару Gibson!",
        chat_id=chat_id,
        user_id=user_id,
        username="guitar_hero",
        message_id=6001,
    )
    await dp.propagate_event(update_type="message", event=msg, bot=mock_bot)

    assert msg.reply.called
    kb = msg.reply.call_args.kwargs.get("reply_markup")
    assert kb is not None
    btn_data = kb.inline_keyboard[0][0].callback_data
    assert btn_data.startswith("forget_fact_6001")

    # Verify stored in both Supabase and Chroma
    assert any("Gibson" in f["text"] for f in chroma_memory.get_all_facts_paged(chat_id)["facts"])

    # Click the forget button as the author
    cb = callback_factory(btn_data, chat_id=chat_id, user_id=user_id, username="guitar_hero")
    await dp.propagate_event(update_type="callback_query", event=cb, bot=mock_bot)

    assert not any("Gibson" in f["text"] for f in chroma_memory.get_all_facts_paged(chat_id)["facts"])


@pytest.mark.asyncio
async def test_combo_all_facts_edit_by_reply_in_supergroup(
    fake_db: SupabaseDB,
    ai_service: Any,
    chroma_memory: ChromaMemoryProvider,
    mock_bot: MagicMock,
    message_factory: Any,
    callback_factory: Any,
) -> None:
    """Tier 3: Editing fact #3 in a supergroup via /all_facts button + reply updates only fact #3."""
    chat_id = -1001234567890
    admin_id = 777
    mock_bot.set_admin(chat_id, admin_id, True)

    chroma_memory.store_single_fact(chat_id, "Факт номер один", "User1")
    chroma_memory.store_single_fact(chat_id, "Факт номер два", "User2")
    chroma_memory.store_single_fact(chat_id, "Факт номер три старый", "User3")

    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))

    msg = message_factory("/all_facts", chat_id=chat_id, user_id=admin_id)
    await dp.propagate_event(update_type="message", event=msg, bot=mock_bot)
    kb = msg.answer.call_args.kwargs["reply_markup"]
    edit_btn_3 = kb.inline_keyboard[2][0]

    cb = callback_factory(edit_btn_3.callback_data, chat_id=chat_id, user_id=admin_id)
    await dp.propagate_event(update_type="callback_query", event=cb, bot=mock_bot)
    prompt_text = cb.message.answer.call_args.args[0]
    assert "Факт номер три старый" in prompt_text

    prompt_msg = message_factory(prompt_text, chat_id=chat_id, user_id=999000, is_bot=True)
    reply_edit = message_factory("Факт номер три обновлённый", chat_id=chat_id, user_id=admin_id, reply_to_message=prompt_msg)
    await dp.propagate_event(update_type="message", event=reply_edit, bot=mock_bot)

    facts = [f["text"] for f in chroma_memory.get_all_facts_paged(chat_id)["facts"]]
    assert "Факт номер три обновлённый" in facts
    assert "Факт номер три старый" not in facts
    assert "Факт номер один" in facts
    assert "Факт номер два" in facts


@pytest.mark.asyncio
async def test_combo_warn_accumulation_auto_mute_then_buy_clear_warns(
    fake_db: SupabaseDB,
    mock_bot: MagicMock,
    ai_service: Any,
    message_factory: Any,
) -> None:
    """Tier 3: Admin warns user twice, user buys item #2 (/buy 2) to clear warns, then a new warn starts at 1/3 instead of muting."""
    from app.bot import admin as admin_mod

    chat_id = -100707
    admin_id = 777
    user_id = 301
    mock_bot.set_admin(chat_id, admin_id, True)

    u = fake_db.get_or_create_user(chat_id, Sender(user_id=user_id, first_name="Player", username="player"))
    fake_db.update_user(u.id, {"reputation": 1000})

    dp = Dispatcher()
    dp.include_router(admin_mod.build_admin_router(mock_bot, fake_db))
    dp.include_router(economy_mod.build_economy_router(fake_db, ai_service))

    target_msg = message_factory("Моё сообщение", chat_id=chat_id, user_id=user_id, first_name="Player", username="player")
    for _ in range(2):
        w = message_factory("/warn флуд", chat_id=chat_id, user_id=admin_id, reply_to_message=target_msg)
        await dp.propagate_event(update_type="message", event=w, bot=mock_bot)

    assert fake_db.get_user_by_platform_id(chat_id, user_id).warns == 2

    # User buys warn clear (/buy 2 costs 600 cookies)
    buy_msg = message_factory("/buy 2", chat_id=chat_id, user_id=user_id, first_name="Player", username="player")
    await dp.propagate_event(update_type="message", event=buy_msg, bot=mock_bot)

    u_after_buy = fake_db.get_user_by_platform_id(chat_id, user_id)
    assert u_after_buy.warns == 0
    assert u_after_buy.reputation == 400

    # Next warn is 1/3, does NOT trigger auto-mute
    w3 = message_factory("/warn ещё раз", chat_id=chat_id, user_id=admin_id, reply_to_message=target_msg)
    await dp.propagate_event(update_type="message", event=w3, bot=mock_bot)
    assert fake_db.get_user_by_platform_id(chat_id, user_id).warns == 1
    assert not mock_bot.restrict_chat_member.called


@pytest.mark.asyncio
async def test_combo_edited_message_hidden_text_link_triggers_warn_and_escalation(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 3: Editing a clean message three times to inject a t.me text_link entity accumulates 3 warns and auto-mutes."""
    chat_id = -100708
    user_id = 302
    fake_db.get_or_create_user(chat_id, Sender(user_id=user_id, first_name="Editor", username="editor"))

    dp = Dispatcher()
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    entities = [SimpleNamespace(type="text_link", url="https://t.me/evil_phishing_channel")]
    for idx in range(3):
        edited = message_factory(
            f"Нажми сюда {idx}",
            chat_id=chat_id,
            user_id=user_id,
            first_name="Editor",
            username="editor",
            entities=entities,
            message_id=7100 + idx,
        )
        await dp.propagate_event(update_type="edited_message", event=edited, bot=mock_bot)
        assert edited.delete.called

    assert mock_bot.restrict_chat_member.called
    assert fake_db.get_user_by_platform_id(chat_id, user_id).warns == 0


@pytest.mark.asyncio
async def test_combo_summary_and_ask_reflect_recent_group_discussion_and_stored_facts(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    chroma_memory: ChromaMemoryProvider,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 3: Group messages are logged, facts are stored via /add_fact, and both /summary and /ask incorporate them."""
    chat_id = -100709
    admin_id = 777
    mock_bot.set_admin(chat_id, admin_id, True)

    captured_calls: list[Any] = []

    async def _mock_comp(**kwargs: Any) -> Any:
        captured_calls.append(kwargs.get("messages"))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="• Обсуждали релиз Python 3.13 и квантовые чипы.", tool_calls=[]), finish_reason="stop")]
        )

    ai_service._chat_completion = AsyncMock(side_effect=_mock_comp)

    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    # Admin adds a fact
    fact_msg = message_factory("/add_fact @dev_lead Главный архитектор серверного ядра", chat_id=chat_id, user_id=admin_id)
    await dp.propagate_event(update_type="message", event=fact_msg, bot=mock_bot)

    # Users chat
    m1 = message_factory("Сегодня тестируем новый релиз Python 3.13 на продакшене", chat_id=chat_id, user_id=401, username="dev_lead")
    await dp.propagate_event(update_type="message", event=m1, bot=mock_bot)

    # /summary
    sum_msg = message_factory("/summary 20", chat_id=chat_id, user_id=401, username="dev_lead")
    await dp.propagate_event(update_type="message", event=sum_msg, bot=mock_bot)
    assert sum_msg.answer.called
    assert "Python 3.13" in str(captured_calls[-1])

    # /ask about @dev_lead
    ask_msg = message_factory("/ask Чем занимается @dev_lead?", chat_id=chat_id, user_id=402, username="curious")
    await dp.propagate_event(update_type="message", event=ask_msg, bot=mock_bot)
    assert ask_msg.answer.called
    assert "архитектор серверного ядра" in str(captured_calls[-1])


@pytest.mark.asyncio
async def test_combo_give_transfers_net_cookies_and_accumulates_chat_jackpot(
    fake_db: SupabaseDB,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Tier 3: Multiple /give transfers deduct 15% tax each time, credit net amount to receivers, and accumulate into chat jackpot."""
    chat_id = -100710
    sender_u = fake_db.get_or_create_user(chat_id, Sender(user_id=101, first_name="Sponsor", username="sponsor"))
    r1 = fake_db.get_or_create_user(chat_id, Sender(user_id=201, first_name="Winner1", username="w1"))
    r2 = fake_db.get_or_create_user(chat_id, Sender(user_id=202, first_name="Winner2", username="w2"))
    fake_db.update_user(sender_u.id, {"reputation": 1000})

    initial_jackpot = fake_db.get_chat_settings(chat_id).casino_jackpot

    dp = Dispatcher()
    dp.include_router(economy_mod.build_economy_router(fake_db, ai_service))

    rep1 = message_factory("Спасибо!", chat_id=chat_id, user_id=201, first_name="Winner1", username="w1")
    g1 = message_factory("/give 200", chat_id=chat_id, user_id=101, first_name="Sponsor", username="sponsor", reply_to_message=rep1)
    await dp.propagate_event(update_type="message", event=g1, bot=mock_bot)

    rep2 = message_factory("Ура!", chat_id=chat_id, user_id=202, first_name="Winner2", username="w2")
    g2 = message_factory("/give 100", chat_id=chat_id, user_id=101, first_name="Sponsor", username="sponsor", reply_to_message=rep2)
    await dp.propagate_event(update_type="message", event=g2, bot=mock_bot)

    # 200 -> tax 30, net 170; 100 -> tax 15, net 85
    assert fake_db.get_user_by_platform_id(chat_id, 101).reputation == 700
    assert fake_db.get_user_by_platform_id(chat_id, 201).reputation == r1.reputation + 170
    assert fake_db.get_user_by_platform_id(chat_id, 202).reputation == r2.reputation + 85
    assert fake_db.get_chat_settings(chat_id).casino_jackpot == initial_jackpot + 45
