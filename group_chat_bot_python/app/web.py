from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.types import Update, BotCommand, BotCommandScopeDefault, InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import FastAPI, HTTPException, Request

from app.bot.admin import build_admin_router
from app.bot.routers.general_admin import build_general_admin_router, make_quiz_question
from app.bot.routers import game_sessions
from app.bot.messages import build_messages_router
from app.bot.rp_commands import build_rp_router
from app.bot.feedback import build_feedback_router
from app.bot.routers.economy import build_economy_router
from app.bot.routers.error import build_error_router
from app.bot.routers.games import build_games_router
from app.bot.routers.profile_ai import build_profile_ai_router
from app.bot.routers.chat_settings import build_chat_settings_router
from app.bot.routers.memes import build_memes_router
from app.config import get_settings
from app.models import Sender
from app.services.ai_service import AIService
from app.services.memory_provider import build_memory_provider
from app.services.persona_service import PersonaService
from app.services.supabase_db import SupabaseDB


def create_app() -> FastAPI:
    settings = get_settings()
    bot = Bot(settings.telegram_bot_token)
    dispatcher = Dispatcher()
    db = SupabaseDB(settings)

    from pathlib import Path
    from app.services.telegram_backup import TelegramBackupService
    data_dir = Path(__file__).resolve().parents[1] / "data" / "chroma_db"
    backup_service = TelegramBackupService(bot, settings.memory_backup_chat_id, data_dir)

    memory = build_memory_provider(settings, db, backup_service)
    persona = PersonaService(db)
    ai_service = AIService(settings, db, memory, persona, bot)

    dispatcher.include_router(build_admin_router(bot, db))
    dispatcher.include_router(build_chat_settings_router(db))
    dispatcher.include_router(build_economy_router(db, ai_service))
    dispatcher.include_router(build_games_router(db, ai_service, settings.bot_name))
    dispatcher.include_router(build_profile_ai_router(db, ai_service, settings.bot_name))
    dispatcher.include_router(build_general_admin_router(db, settings.bot_name, ai_service))
    dispatcher.include_router(build_rp_router(db))
    dispatcher.include_router(build_memes_router(db))
    dispatcher.include_router(build_feedback_router(bot, db))
    dispatcher.include_router(build_messages_router(bot, settings, db, ai_service))
    dispatcher.include_router(build_error_router())

    @dispatcher.message.outer_middleware()
    async def activity_middleware(handler, event, data):
        # Обновляем время активности при каждом сообщении
        chat_type = getattr(event.chat, "type", "")
        chat_type_value = getattr(chat_type, "value", str(chat_type))
        if chat_type_value in {"group", "supergroup"}:
            db.mark_chat_active(event.chat.id)
        return await handler(event, data)

    reminder_task: asyncio.Task | None = None
    auto_event_task: asyncio.Task | None = None
    backup_task: asyncio.Task | None = None

    async def backup_loop() -> None:
        try:
            while True:
                await asyncio.sleep(12 * 3600)
                if backup_service:
                    await backup_service.upload_backup("💾 Периодический авто-бэкап памяти (каждые 12 часов)")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[backup_loop error] {e}")

    async def reminders_loop() -> None:
        try:
            while True:
                try:
                    for reminder in db.get_due_reminders():
                        mention = reminder.user_name or f"ID: {reminder.user_id}"
                        await bot.send_message(
                            reminder.chat_id,
                            f"Напоминание для {mention}:\n\n{reminder.text}",
                        )
                        db.remove_reminder(reminder.id)
                except Exception as e:
                    print(f"[reminders_loop_error] {e}")
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    async def auto_events_loop() -> None:
        try:
            while True:
                await asyncio.sleep(1800)
                try:
                    active_chats = db.get_active_chats_for_events(hours=12)
                    for chat_id in active_chats:
                        await try_trigger_random_event(bot, db, chat_id, ai_service)
                except Exception as e:
                    print(f"[auto_events_loop_error] {e}")
        except asyncio.CancelledError:
            pass

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal reminder_task, auto_event_task, backup_task
        me = await bot.get_me()
        settings.bot_username = me.username
        try:
            await configure_webhook()
        except Exception:
            pass
        reminder_task = asyncio.create_task(reminders_loop())
        auto_event_task = asyncio.create_task(auto_events_loop())
        backup_task = asyncio.create_task(backup_loop())
        yield
        if backup_task:
            backup_task.cancel()
        if auto_event_task:
            auto_event_task.cancel()
        if reminder_task:
            reminder_task.cancel()
        await bot.session.close()

    async def configure_webhook() -> None:
        base_url = settings.render_external_url.rstrip("/")
        if not base_url.startswith("http"):
            return
        webhook_url = f"{base_url}/bot{settings.telegram_bot_token}"
        await bot.set_webhook(
            webhook_url,
            secret_token=settings.webhook_secret_token,
            allowed_updates=["message", "message_reaction", "poll_answer", "callback_query", "chat_member"],
            drop_pending_updates=False,
        )
        
        commands = [
            BotCommand(command="help", description="✨ Главное меню и помощь"),
            BotCommand(command="card", description="👤 Персональная карточка участника"),
            BotCommand(command="me", description="📊 Мой профиль и баланс"),
            BotCommand(command="lore", description="📜 Легенды и Лента событий чата"),
            BotCommand(command="daily", description="🍪 Ежедневный бонус"),
            BotCommand(command="top", description="🏆 Рейтинг участников"),
            BotCommand(command="aiimage", description="🎨 Сгенерировать ИИ-картинку"),
            BotCommand(command="signai", description="✍️ ИИ-сигна с Никой"),
            BotCommand(command="remember", description="🧠 Запомнить факт в память"),
            BotCommand(command="forget_me", description="🧹 Стереть факты о себе"),
            BotCommand(command="shop", description="🛒 Магазин печенек"),
            BotCommand(command="debts", description="📒 Долги и должники"),
            BotCommand(command="jail", description="🚔 Статус тюрьмы"),
        ]
        try:
            await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        except Exception as e:
            print(f"[set_my_commands error] {e}")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal reminder_task, auto_event_task
        me = await bot.get_me()
        settings.bot_username = me.username
        try:
            await configure_webhook()
        except Exception:
            pass
        reminder_task = asyncio.create_task(reminders_loop())
        auto_event_task = asyncio.create_task(auto_events_loop())
        yield
        if auto_event_task:
            auto_event_task.cancel()
            try:
                await auto_event_task
            except Exception:
                pass
        if reminder_task:
            reminder_task.cancel()
            try:
                await reminder_task
            except Exception:
                pass
        await bot.session.close()

    app = FastAPI(lifespan=lifespan)
    app.state.bot = bot
    app.state.dispatcher = dispatcher
    app.state.db = db
    app.state.memory = memory
    app.state.settings = settings

    next_auto_drop: dict[int, float] = {}
    next_auto_quiz: dict[int, float] = {}

    async def auto_events_loop() -> None:
        try:
            while True:
                await asyncio.sleep(60)
                now = asyncio.get_running_loop().time()

                # Очистка устаревших сессий авто-дропов и авто-викторин (старше 30 минут)
                now_ts = time.time()
                for key in list(game_sessions.AUTO_DROP_SESSIONS):
                    session = game_sessions.AUTO_DROP_SESSIONS.get(key)
                    if session and now_ts - int(session.get("created_at", 0)) > 30 * 60:
                        game_sessions.AUTO_DROP_SESSIONS.pop(key, None)
                for key in list(game_sessions.AUTO_QUIZ_SESSIONS):
                    session = game_sessions.AUTO_QUIZ_SESSIONS.get(key)
                    if session and now_ts - int(session.get("created_at", 0)) > 30 * 60:
                        game_sessions.AUTO_QUIZ_SESSIONS.pop(key, None)
                active_chat_ids = db.get_active_chat_ids(minutes=10, limit=30)
                for chat_id in list(next_auto_drop):
                    if chat_id not in active_chat_ids:
                        next_auto_drop.pop(chat_id, None)
                        next_auto_quiz.pop(chat_id, None)
                for chat_id in active_chat_ids:
                    try:
                        next_auto_drop.setdefault(chat_id, now + 35 * 60 + random.randint(0, 10 * 60))
                        next_auto_quiz.setdefault(chat_id, now + 45 * 60 + random.randint(0, 15 * 60))

                        chat_settings = db.get_chat_settings(chat_id)
                        if now >= next_auto_drop[chat_id]:
                            if chat_settings.auto_drop_enabled:
                                await send_auto_drop(chat_id)
                            next_auto_drop[chat_id] = now + 35 * 60 + random.randint(0, 15 * 60)
                        if now >= next_auto_quiz[chat_id]:
                            if chat_settings.auto_quiz_enabled:
                                await send_auto_quiz(chat_id)
                            next_auto_quiz[chat_id] = now + 45 * 60 + random.randint(0, 20 * 60)
                    except Exception as exc:
                        print(f"[auto_events:error] chat_id={chat_id} error={exc}")
        except asyncio.CancelledError:
            pass

    async def send_auto_drop(chat_id: int) -> None:
        reward = random.randint(10, 28)
        created_at = int(time.time())
        msg = await bot.send_message(
            chat_id,
            "🍪 <b>Печенька сама упала в чат!</b>\nКто первый нажмет, тот заберет награду.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🍪 Забрать {reward}", callback_data=f"drop_claim_{reward}_{created_at}")]
            ]),
        )
        game_sessions.AUTO_DROP_SESSIONS[f"{chat_id}_{msg.message_id}"] = {
            "reward": reward,
            "created_at": created_at,
        }

    async def send_auto_quiz(chat_id: int) -> None:
        quiz = make_quiz_question()
        created_at = int(time.time())
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=str(quiz["options"][0]), callback_data=f"quiz_answer_0_{quiz['answer_idx']}_{quiz['reward']}_{created_at}_{quiz['options'][quiz['answer_idx']]}"),
                InlineKeyboardButton(text=str(quiz["options"][1]), callback_data=f"quiz_answer_1_{quiz['answer_idx']}_{quiz['reward']}_{created_at}_{quiz['options'][quiz['answer_idx']]}"),
            ],
            [
                InlineKeyboardButton(text=str(quiz["options"][2]), callback_data=f"quiz_answer_2_{quiz['answer_idx']}_{quiz['reward']}_{created_at}_{quiz['options'][quiz['answer_idx']]}"),
                InlineKeyboardButton(text=str(quiz["options"][3]), callback_data=f"quiz_answer_3_{quiz['answer_idx']}_{quiz['reward']}_{created_at}_{quiz['options'][quiz['answer_idx']]}"),
            ],
        ])
        msg = await bot.send_message(
            chat_id,
            f"🧠 <b>Авто-викторина</b>\n\n"
            f"Сколько будет: <code>{quiz['question']}</code>?\n"
            f"Первый правильный ответ забирает <b>{quiz['reward']}</b> 🍪.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        game_sessions.AUTO_QUIZ_SESSIONS[f"{chat_id}_{msg.message_id}"] = {
            **quiz,
            "created_at": created_at,
            "wrong_users": set(),
        }

    @app.get("/")
    async def root() -> dict:
        return {"status": "running", "bot": settings.bot_name}

    @app.head("/")
    async def root_head() -> None:
        return None

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "memory": await memory.health()}

    @app.head("/health")
    async def health_head() -> None:
        return None

    @app.get("/api/memory/health")
    async def memory_health() -> dict:
        return await memory.health()

    @app.post(f"/bot{settings.telegram_bot_token}")
    async def telegram_webhook(request: Request) -> dict:
        if request.headers.get("x-telegram-bot-api-secret-token") != settings.webhook_secret_token:
            raise HTTPException(status_code=403, detail="Invalid secret token")

        try:
            payload = await request.json()
            if payload.get("message_reaction"):
                await handle_message_reaction(payload["message_reaction"], db)
            
            if payload.get("poll_answer"):
                await handle_poll_answer(payload["poll_answer"], db)

            update = Update.model_validate(payload, context={"bot": bot})
            await dispatcher.feed_update(bot, update)
        except Exception as exc:
            print(f"[WEBHOOK:error] error={exc}")
        return {"ok": True}

    return app


async def handle_message_reaction(payload: dict, db: SupabaseDB) -> None:
    chat = payload.get("chat") or {}
    chat_id = chat.get("id")
    message_id = payload.get("message_id")
    if not chat_id or not message_id:
        return

    author_id = db.get_message_author(chat_id, message_id)
    if not author_id:
        return

    actor = payload.get("user") or payload.get("actor_chat") or {}
    actor_id = actor.get("id")
    if not actor_id or actor_id == author_id:
        return

    old_reaction = payload.get("old_reaction") or []
    new_reaction = payload.get("new_reaction") or []
    delta = _reaction_score(new_reaction) - _reaction_score(old_reaction)
    if delta == 0:
        return

    author = db.get_user_by_platform_id(chat_id, author_id)
    if not author:
        return
    db.update_user(author.id, {"reputation": author.reputation + delta})


async def handle_poll_answer(payload: dict, db: SupabaseDB) -> None:
    poll_id = str(payload.get("poll_id") or "")
    option_ids = payload.get("option_ids") or []
    user_data = payload.get("user") or {}
    user_id = int(user_data.get("id") or 0)
    poll_data = db.get_poll_data(poll_id)
    if not poll_id or not poll_data or not user_id:
        return

    options = poll_data.get("options") or []
    selected_options = [str(options[idx]) for idx in option_ids if isinstance(idx, int) and 0 <= idx < len(options)]
    if not selected_options:
        return

    sender = Sender(
        user_id=user_id,
        first_name=str(user_data.get("first_name") or "User"),
        username=user_data.get("username"),
        is_bot=False,
    )
    options_text = ", ".join(selected_options)
    question = str(poll_data.get("question") or "опрос")
    db.store_message_context(
        chat_id=int(poll_data["chat_id"]),
        message_id=int(time.time() * 1000) % 1_000_000_000,
        sender=sender,
        text=f"[ГОЛОС] {sender.display_name} выбрал(а) '{options_text}' в опросе: {question}",
        message_type="poll_vote",
    )


def _reaction_score(reactions: list[dict]) -> int:
    if not reactions:
        return 0
    emoji = reactions[0].get("emoji")
    if emoji in {"👎", "💩", "🤮"}:
        return -1
    return 1 if emoji else 0