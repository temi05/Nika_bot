import asyncio
import random
import time
from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.services.ai_service import AIService
from app.services.supabase_db import SupabaseDB
from app.bot.routers.jail_helpers import (
    deny_if_jailed,
)
from app.utils import escape_html, get_sender_data


def _max_game_bet(user) -> int:
    if user.reputation <= 0:
        return 0
    # Базовый лимит 35% от баланса
    balance_cap = max(1, int(user.reputation * 0.35))
    # Абсолютный максимум 15 000 печенек (лимит снижен для борьбы с инфляцией)
    return min(user.reputation, 15000, balance_cap)





def build_games_router(db: SupabaseDB, ai: AIService, bot_name: str) -> Router:
    router = Router(name="games")

    async def _games_closed(message: Message) -> None:
        await message.answer(
            "🛑 <b>Игры официально ЗАКРЫТЫ!</b>\n\n"
            "Ника: <i>«Всё, хватит с меня этого шума и спама! Все автоматы, кубики и шахты отправлены на свалку. "
            "Никаких больше игр! Идите работать... или просто общайтесь!»</i>",
            parse_mode="HTML"
        )

    # Список всех игровых команд, которые теперь отключены
    game_commands = [
        "coin", "flip", "монетка", 
        "dice", "кубики", "кости", 
        "diceduel", "кубодуэль", "дайсдуэль", 
        "rps", "кнб", 
        "duel", "дуэль", 
        "fish", "рыбалка", 
        "box", "коробка", "сундук", 
        "scratch", "скретч", "лотерейка", 
        "mine", "шахта", "копать", 
        "dailyquest", "квест", "дейлик",
        "casino", "gamble", "spin", "казино", "ставка", 
        "tower", "башня", "climb"
    ]

    @router.message(Command(*game_commands))
    async def disabled_games_handler(message: Message) -> None:
        await _games_closed(message)
    
    return router
