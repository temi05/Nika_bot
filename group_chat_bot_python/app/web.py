from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request

from app.bot.admin import build_admin_router
from app.bot.commands import build_commands_router
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
    dispatcher.include_router(build_feedback_router(db))
    dispatcher.include_router(build_messages_router(bot, settings, db, ai_service))

    reminder_task: asyncio.Task | None = None

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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal reminder_task
        me = await bot.get_me()
        settings.bot_username = me.username
        try:
            await configure_webhook()
        except Exception:
            pass
        reminder_task = asyncio.create_task(reminders_loop())
        yield
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

    @app.get("/")
    async def root() -> dict:
        return {"status": "running", "bot": settings.bot_name}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "memory": await memory.health()}

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
