from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.types import Update, BotCommand, BotCommandScopeDefault, InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import FastAPI, HTTPException, Request

from app.bot.admin import build_admin_router
from app.bot.commands import AUTO_DROP_SESSIONS, AUTO_QUIZ_SESSIONS, build_commands_router, make_quiz_question
from app.bot.messages import build_messages_router
from app.bot.rp_commands import build_rp_router
from app.bot.feedback import build_feedback_router
from app.config import get_settings
from app.services.ai_service import AIService
from app.services.memory_provider import build_memory_provider
from app.services.persona_service import PersonaService
from app.services.supabase_db import SupabaseDB


def create_app() -> FastAPI:
    settings = get_settings()
    bot = Bot(settings.telegram_bot_token)
    dispatcher = Dispatcher()
    db = SupabaseDB(settings)
    memory = build_memory_provider(settings, db)
    persona = PersonaService(db)
    ai_service = AIService(settings, db, memory, persona, bot)

    dispatcher.include_router(build_admin_router(bot, db))
    dispatcher.include_router(build_commands_router(db, settings.bot_name, ai_service))
    dispatcher.include_router(build_rp_router(db))
    dispatcher.include_router(build_feedback_router(bot, db))
    dispatcher.include_router(build_messages_router(bot, settings, db, ai_service))

    @dispatcher.message.outer_middleware()
    async def activity_middleware(handler, event, data):
        # Обновляем время активности при каждом сообщении
        db = data.get("db")
        if db:
            db.mark_chat_active(event.chat.id)
        return await handler(event, data)

    reminder_task: asyncio.Task | None = None
    auto_event_task: asyncio.Task | None = None

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
                        db.mark_reminder_sent(reminder.id)
                except Exception:
                    pass
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass

    async def configure_webhook() -> None:
        base_url = settings.render_external_url.rstrip("/")
        if not base_url.startswith("http"):
            return
        webhook_url = f"{base_url}/bot{settings.telegram_bot_token}"
        await bot.set_webhook(
            webhook_url,
            secret_token=settings.webhook_secret_token,
            allowed_updates=["message", "message_reaction", "callback_query", "chat_member"],
            drop_pending_updates=False,
        )
        
        commands = [
            BotCommand(command="help", description="✨ Главное меню и помощь"),
            BotCommand(command="me", description="👤 Мой профиль и баланс"),
            BotCommand(command="daily", description="🍪 Собрать ежедневный бонус"),
            BotCommand(command="top", description="🏆 Рейтинг участников"),
            BotCommand(command="casino", description="🎰 Крутить рулетку (ставка)"),
            BotCommand(command="coin", description="🪙 Орел или решка"),
            BotCommand(command="rps", description="✊ Камень-ножницы-бумага"),
            BotCommand(command="fish", description="🎣 Рыбалка за печеньками"),
            BotCommand(command="shop", description="🛒 Магазин печенек"),
            BotCommand(command="loan", description="🤝 Предложить в долг"),
            BotCommand(command="ask_loan", description="🙏 Попросить в долг"),
            BotCommand(command="repay", description="💰 Вернуть долг"),
            BotCommand(command="debts", description="📒 Долги и должники"),
            BotCommand(command="jail", description="🚔 Статус тюрьмы"),
            BotCommand(command="bail", description="🔓 Оплатить залог"),
            BotCommand(command="give", description="🎁 Подарить печеньки"),
            BotCommand(command="steal", description="🕵️ Попробовать украсть"),
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

    last_auto_drop: dict[int, float] = {}
    last_auto_quiz: dict[int, float] = {}

    async def auto_events_loop() -> None:
        try:
            while True:
                await asyncio.sleep(60)
                now = asyncio.get_running_loop().time()
                for chat_id in db.get_active_chat_ids(minutes=180, limit=30):
                    try:
                        if now - last_auto_drop.get(chat_id, 0.0) >= 25 * 60 and random.random() < 0.35:
                            await send_auto_drop(chat_id)
                            last_auto_drop[chat_id] = now
                        if now - last_auto_quiz.get(chat_id, 0.0) >= 35 * 60 and random.random() < 0.25:
                            await send_auto_quiz(chat_id)
                            last_auto_quiz[chat_id] = now
                    except Exception as exc:
                        print(f"[auto_events:error] chat_id={chat_id} error={exc}")
        except asyncio.CancelledError:
            pass

    async def send_auto_drop(chat_id: int) -> None:
        reward = random.randint(12, 35)
        msg = await bot.send_message(
            chat_id,
            "🍪 <b>Печенька сама упала в чат!</b>\nКто первый нажмет, тот заберет награду.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🍪 Забрать {reward}", callback_data="drop_claim")]
            ]),
        )
        AUTO_DROP_SESSIONS[f"{chat_id}_{msg.message_id}"] = {
            "reward": reward,
            "created_at": time.time(),
        }

    async def send_auto_quiz(chat_id: int) -> None:
        quiz = make_quiz_question()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=str(quiz["options"][0]), callback_data="quiz_answer_0"),
                InlineKeyboardButton(text=str(quiz["options"][1]), callback_data="quiz_answer_1"),
            ],
            [
                InlineKeyboardButton(text=str(quiz["options"][2]), callback_data="quiz_answer_2"),
                InlineKeyboardButton(text=str(quiz["options"][3]), callback_data="quiz_answer_3"),
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
        AUTO_QUIZ_SESSIONS[f"{chat_id}_{msg.message_id}"] = {
            **quiz,
            "created_at": time.time(),
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


def _reaction_score(reactions: list[dict]) -> int:
    if not reactions:
        return 0
    emoji = reactions[0].get("emoji")
    if emoji in {"👎", "💩", "🤮"}:
        return -1
    return 1 if emoji else 0
