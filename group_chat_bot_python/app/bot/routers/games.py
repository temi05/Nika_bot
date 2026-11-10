from __future__ import annotations

from aiogram import Router
from app.services.ai_service import AIService
from app.services.supabase_db import SupabaseDB


def build_games_router(db: SupabaseDB, ai: AIService, bot_name: str) -> Router:
    router = Router(name="games")
    return router
