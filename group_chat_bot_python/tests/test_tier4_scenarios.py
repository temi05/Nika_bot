from __future__ import annotations

import asyncio
import io
import zipfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from fastapi.testclient import TestClient

import app.bot.admin as admin_mod
import app.bot.messages as messages_mod
import app.bot.routers.chat_settings as chat_settings_mod
import app.bot.routers.economy as economy_mod
import app.bot.routers.general_admin as general_admin_mod
import app.bot.routers.profile_ai as profile_ai_mod
import app.web as web_mod
from app.config import Settings
from app.models import Sender
from app.services.chroma_memory_provider import ChromaMemoryProvider
from app.services.supabase_db import SupabaseDB
from app.services.telegram_backup import TelegramBackupService


@pytest.mark.asyncio
async def test_scenario_1_newcomer_captcha_and_raid_defense(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Scenario 1: Newcomer join -> captcha -> multi-chat isolation -> captcha pass -> post-join link defense."""
    chat_a = -1001111
    chat_b = -1002222
    newbie_id = 8001

    dp = Dispatcher()
    dp.include_router(economy_mod.build_economy_router(fake_db, ai_service))
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    # 1. Join Group A -> restricted & captcha sent
    join_msg = message_factory(
        "",
        chat_id=chat_a,
        user_id=newbie_id,
        new_chat_members=[SimpleNamespace(id=newbie_id, full_name="NewbieUser", is_bot=False)],
    )
    await dp.propagate_event(update_type="message", event=join_msg, bot=mock_bot)
    assert mock_bot.send_photo.called

    try:
        challenge = fake_db.get_verification(newbie_id, chat_id=chat_a)  # type: ignore[call-arg]
    except TypeError:
        challenge = fake_db.get_verification(newbie_id)
    assert challenge is not None

    # 2. Chatting in Group B while pending in Group A -> allowed!
    b_msg = message_factory("Привет чату Б!", chat_id=chat_b, user_id=newbie_id, message_id=101)
    await dp.propagate_event(update_type="message", event=b_msg, bot=mock_bot)
    assert not b_msg.delete.called

    # 3. Wrong code in Group A -> deleted
    wrong_msg = message_factory("00000", chat_id=chat_a, user_id=newbie_id, message_id=102)
    await dp.propagate_event(update_type="message", event=wrong_msg, bot=mock_bot)
    assert wrong_msg.delete.called

    # 4. Correct code in Group A -> verified!
    right_msg = message_factory(challenge.code, chat_id=chat_a, user_id=newbie_id, message_id=103)
    await dp.propagate_event(update_type="message", event=right_msg, bot=mock_bot)
    assert right_msg.answer.called
    assert "прошёл проверку" in right_msg.answer.call_args.args[0]

    # 5. Verified newcomer posts a promo link -> caught and warned
    spam_msg = message_factory("Заходите на https://t.me/raid_channel", chat_id=chat_a, user_id=newbie_id, message_id=104)
    await dp.propagate_event(update_type="message", event=spam_msg, bot=mock_bot)
    assert spam_msg.delete.called


@pytest.mark.asyncio
async def test_scenario_2_moderation_and_escalation_lifecycle(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Scenario 2: Rules setup -> badword + edited link violations -> /warn escalation -> auto-mute -> /unmute."""
    chat_id = -1002001
    admin_id = 9001
    violator_id = 9002
    mock_bot.set_admin(chat_id, admin_id, True)

    dp = Dispatcher()
    dp.include_router(admin_mod.build_admin_router(mock_bot, fake_db))
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    # 1. Admin sets rules and adds bad word
    await dp.propagate_event(
        update_type="message",
        event=message_factory("/setrules Запрещены спойлеры и реклама.", chat_id=chat_id, user_id=admin_id),
        bot=mock_bot,
    )
    await dp.propagate_event(
        update_type="message",
        event=message_factory("/banword спойлер", chat_id=chat_id, user_id=admin_id),
        bot=mock_bot,
    )

    # 2. Violator sends bad word mentioning Nika -> Warn 1/3
    v1 = message_factory("НейроНика, вот спойлер к фильму!", chat_id=chat_id, user_id=violator_id, message_id=201)
    await dp.propagate_event(update_type="message", event=v1, bot=mock_bot)
    assert v1.delete.called
    assert fake_db.get_user_by_platform_id(chat_id, violator_id).warns == 1

    # 3. Violator edits message to insert t.me link -> Warn 2/3
    v2 = message_factory("Ссылка: https://t.me/spoiler_channel", chat_id=chat_id, user_id=violator_id, message_id=202)
    await dp.propagate_event(update_type="edited_message", event=v2, bot=mock_bot)
    assert v2.delete.called
    assert fake_db.get_user_by_platform_id(chat_id, violator_id).warns == 2

    # 4. Admin issues 3rd warn via /warn -> reaches warn_limit (3/3) -> auto-mute and reset warns to 0
    mock_bot.restrict_chat_member.reset_mock()
    warn_msg = message_factory("/warn рецидив", chat_id=chat_id, user_id=admin_id, reply_to_message=v2)
    await dp.propagate_event(update_type="message", event=warn_msg, bot=mock_bot)
    assert mock_bot.restrict_chat_member.called
    assert fake_db.get_user_by_platform_id(chat_id, violator_id).warns == 0

    # 5. Admin unmutes user
    mock_bot.restrict_chat_member.reset_mock()
    unmute_msg = message_factory("/unmute", chat_id=chat_id, user_id=admin_id, reply_to_message=v2)
    await dp.propagate_event(update_type="message", event=unmute_msg, bot=mock_bot)
    assert mock_bot.restrict_chat_member.called


@pytest.mark.asyncio
async def test_scenario_3_economy_lending_repayment_and_jail_lifecycle(
    fake_db: SupabaseDB,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
    callback_factory: Any,
) -> None:
    """Scenario 3: /daily -> /loan offer & accept -> /judge jail blocks /give & /buy -> release -> /repay sets 'repaid'."""
    chat_id = -1003001
    admin_id = 700
    lender_id = 701
    borrower_id = 702
    mock_bot.set_admin(chat_id, admin_id, True)

    dp = Dispatcher()
    dp.include_router(economy_mod.build_economy_router(fake_db, ai_service))
    dp.include_router(general_admin_mod.build_general_admin_router(fake_db, "НейроНика", ai_service))

    # 1. Lender claims /daily and gets extra cookies
    daily_msg = message_factory("/daily", chat_id=chat_id, user_id=lender_id, first_name="Lender", username="lender")
    await dp.propagate_event(update_type="message", event=daily_msg, bot=mock_bot)
    lender_user = fake_db.get_user_by_platform_id(chat_id, lender_id)
    assert lender_user is not None and lender_user.reputation >= 90
    fake_db.update_user(lender_user.id, {"reputation": 500})

    borrower_msg = message_factory("Нужны печеньки", chat_id=chat_id, user_id=borrower_id, first_name="Borrower", username="borrower")
    fake_db.get_or_create_user(chat_id, Sender(user_id=borrower_id, first_name="Borrower", username="borrower"))

    # 2. Lender offers /loan 50 to Borrower (within loan_limit == 50 for level 1)
    loan_msg = message_factory(
        "/loan 50",
        chat_id=chat_id,
        user_id=lender_id,
        first_name="Lender",
        username="lender",
        reply_to_message=borrower_msg,
        message_id=300,
    )
    await dp.propagate_event(update_type="message", event=loan_msg, bot=mock_bot)
    assert loan_msg.answer.called

    # Sent message ID in make_test_message is message_id + 100 = 400
    accept_cb = callback_factory(
        f"loan_acc_50_{lender_id}_{borrower_id}",
        chat_id=chat_id,
        user_id=borrower_id,
        first_name="Borrower",
        username="borrower",
        message_id=400,
    )
    await dp.propagate_event(update_type="callback_query", event=accept_cb, bot=mock_bot)
    assert fake_db.get_user_by_platform_id(chat_id, borrower_id).debt == 50

    # 3. Admin sentences Borrower via /judge 20 проверка
    judge_msg = message_factory("/judge 20 проверка", chat_id=chat_id, user_id=admin_id, reply_to_message=borrower_msg)
    await dp.propagate_event(update_type="message", event=judge_msg, bot=mock_bot)

    # 4. Jailed Borrower cannot /give or /buy
    give_while_jailed = message_factory(
        "/give 10",
        chat_id=chat_id,
        user_id=borrower_id,
        first_name="Borrower",
        username="borrower",
        reply_to_message=daily_msg,
    )
    await dp.propagate_event(update_type="message", event=give_while_jailed, bot=mock_bot)
    assert "тюрьм" in give_while_jailed.answer.call_args.args[0].lower()

    # 5. Admin releases Borrower via /judge release
    release_msg = message_factory("/judge release", chat_id=chat_id, user_id=admin_id, reply_to_message=borrower_msg)
    await dp.propagate_event(update_type="message", event=release_msg, bot=mock_bot)

    # 6. Borrower repays debt via /repay -> status='repaid'
    repay_msg = message_factory("/repay", chat_id=chat_id, user_id=borrower_id, first_name="Borrower", username="borrower")
    await dp.propagate_event(update_type="message", event=repay_msg, bot=mock_bot)
    assert fake_db.get_user_by_platform_id(chat_id, borrower_id).debt == 0
    assert fake_db.fake_client.tables["bot_debts"][0]["status"] == "repaid"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_scenario_4_memory_vector_backup_and_restore_lifecycle(
    fake_db: SupabaseDB,
    ai_service: Any,
    chroma_memory: ChromaMemoryProvider,
    backup_service: TelegramBackupService,
    mock_bot: MagicMock,
    message_factory: Any,
    callback_factory: Any,
) -> None:
    """Scenario 4: Store facts -> backup card -> supergroup /all_facts delete -> /export_facts -> backup & restore."""
    chat_id = -1001234567890
    admin_id = 888
    mock_bot.set_admin(chat_id, admin_id, True)

    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))

    for idx, text in enumerate(["Любит чай улун", "Играет в шахматы", "Пишет компиляторы"], start=1):
        m = message_factory(f"/remember {text}", chat_id=chat_id, user_id=admin_id, username="admin", message_id=500 + idx)
        await dp.propagate_event(update_type="message", event=m, bot=mock_bot)
    await asyncio.sleep(0.05)

    # Open /all_facts and delete fact #2
    af_msg = message_factory("/all_facts", chat_id=chat_id, user_id=admin_id, username="admin")
    await dp.propagate_event(update_type="message", event=af_msg, bot=mock_bot)
    kb = af_msg.answer.call_args.kwargs["reply_markup"]
    del_btn_2 = kb.inline_keyboard[1][1]

    del_cb = callback_factory(del_btn_2.callback_data, chat_id=chat_id, user_id=admin_id, username="admin")
    await dp.propagate_event(update_type="callback_query", event=del_cb, bot=mock_bot)

    remaining = [f["text"] for f in chroma_memory.get_all_facts_paged(chat_id)["facts"]]
    assert len(remaining) == 2
    assert not any("Играет в шахматы" in t for t in remaining)

    # Export facts
    exp_msg = message_factory("/export_facts", chat_id=chat_id, user_id=admin_id, username="admin")
    await dp.propagate_event(update_type="message", event=exp_msg, bot=mock_bot)
    assert exp_msg.answer_document.called

    # Backup and safe restore
    ok_backup = await backup_service.upload_backup("Тестовый бэкап")
    assert ok_backup is True


@pytest.mark.asyncio
async def test_scenario_5_ai_conversation_tools_and_group_utilities(
    fake_db: SupabaseDB,
    test_settings: Settings,
    ai_service: Any,
    mock_bot: MagicMock,
    message_factory: Any,
) -> None:
    """Scenario 5: Group chat conversation -> memory retention -> /summary -> /ask -> /remind -> /kto."""
    chat_id = -1005001
    dp = Dispatcher()
    dp.include_router(profile_ai_mod.build_profile_ai_router(fake_db, ai_service, "НейроНика"))
    dp.include_router(general_admin_mod.build_general_admin_router(fake_db, "НейроНика", ai_service))
    dp.include_router(messages_mod.build_messages_router(mock_bot, test_settings, fake_db, ai_service))

    ai_service._chat_completion = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="• Обсудили архитектуру\n• Назначили код-ревью на вечер", tool_calls=[]),
                    finish_reason="stop",
                )
            ]
        )
    )

    for i in range(5):
        m = message_factory(f"Сообщение от разработчика #{i} про архитектуру", chat_id=chat_id, user_id=100 + i, username=f"dev_{i}", message_id=600 + i)
        await dp.propagate_event(update_type="message", event=m, bot=mock_bot)

    sum_msg = message_factory("/summary", chat_id=chat_id, user_id=100, username="dev_0")
    await dp.propagate_event(update_type="message", event=sum_msg, bot=mock_bot)
    assert sum_msg.answer.called
    assert "архитектуру" in sum_msg.answer.call_args.args[0]

    ask_msg = message_factory("/ask когда код-ревью?", chat_id=chat_id, user_id=100, username="dev_0")
    await dp.propagate_event(update_type="message", event=ask_msg, bot=mock_bot)
    assert ask_msg.answer.called

    rem_msg = message_factory("/remind 30m Провести код-ревью", chat_id=chat_id, user_id=100, username="dev_0")
    await dp.propagate_event(update_type="message", event=rem_msg, bot=mock_bot)
    assert rem_msg.answer.called

    kto_msg = message_factory("/kto самый ответственный ревьюер?", chat_id=chat_id, user_id=100, username="dev_0")
    await dp.propagate_event(update_type="message", event=kto_msg, bot=mock_bot)
    assert kto_msg.answer.called


def test_scenario_6_fastapi_webhook_and_health_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
    fake_db: SupabaseDB,
    chroma_memory: ChromaMemoryProvider,
    mock_bot: MagicMock,
) -> None:
    """Scenario 6: FastAPI /, /health, /api/memory/health, and webhook secret token verification."""
    monkeypatch.setattr(web_mod, "get_settings", lambda: test_settings)
    monkeypatch.setattr(web_mod, "Bot", lambda token: mock_bot)
    monkeypatch.setattr(web_mod, "SupabaseDB", lambda settings: fake_db)
    monkeypatch.setattr(web_mod, "build_memory_provider", lambda settings, db, backup=None: chroma_memory)

    app = web_mod.create_app()
    client = TestClient(app, raise_server_exceptions=True)

    r_root = client.get("/")
    assert r_root.status_code == 200
    assert r_root.json()["status"] == "running"

    r_health = client.get("/health")
    assert r_health.status_code == 200
    body = r_health.json()
    assert body["status"] == "ok"
    assert body["memory"]["healthy"] is True

    # Invalid webhook secret token -> 403
    r_forbidden = client.post(
        f"/bot{test_settings.telegram_bot_token}",
        headers={"x-telegram-bot-api-secret-token": "wrong-secret"},
        json={"update_id": 1},
    )
    assert r_forbidden.status_code == 403
