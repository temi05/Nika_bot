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

    casino_commands = ["casino", "gamble", "spin", "казино", "ставка"]

    async def _games_closed(message: Message) -> None:
        await message.answer(
            "🛑 <b>Игры официально ЗАКРЫТЫ!</b>\n\n"
            "Ника: <i>«Всё, хватит с меня этого шума и спама! Все автоматы, кубики и шахты отправлены на свалку. "
            "Никаких больше игр! Идите работать... или просто общайтесь!»</i>",
            parse_mode="HTML"
        )

    @router.message(Command(*casino_commands))
    async def casino_prank_handler(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        raw_bet = (command.args or "").strip()
        bet_text = escape_html(raw_bet) if raw_bet else "всё, что осталось от надежды"
        jackpot = db.get_chat_settings(message.chat.id).casino_jackpot

        await message.answer(
            "🎰 <b>КАЗИНО NEURONIKA: запуск...</b>\n\n"
            f"Игрок: <b>{escape_html(sender.display_name)}</b>\n"
            f"Ставка: <code>{bet_text}</code>\n"
            f"Джекпот за стеклом: <b>{jackpot} 🍪</b>\n\n"
            "<code>[■■■■■■■■□]</code> 97%\n"
            "<i>Автомат подозрительно ожил. Ника делает вид, что ничего не видит.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎰 Крутить",
                        callback_data=f"casino_prank_spin_{sender.user_id}",
                    )
                ]
            ]),
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("casino_prank_spin_"))
    async def casino_prank_spin(query: CallbackQuery) -> None:
        if not query.message or not query.data:
            return

        try:
            original_user_id = int(query.data.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            await query.answer("Автомат уже делает вид, что не понимает эту кнопку.", show_alert=True)
            return

        if query.from_user.id != original_user_id:
            await query.answer("Не твой рычаг. Чужую зависимость не трогаем.", show_alert=True)
            return

        user = query.from_user
        display_name = f"@{user.username}" if user.username else (user.first_name or "Инкогнито")
        punchlines = [
            "Выпало: <code>🍒 | 🍒 | НЕТ</code>",
            "Выпало: <code>7 | 7 | санитарный запрет</code>",
            "Выпало: <code>🍪 | 🍪 | лекция о финансовой грамотности</code>",
            "Выпало: <code>💎 | 💎 | закрыто по просьбе Ники</code>",
        ]

        await query.message.edit_text(
            "🚨 <b>ПОЙМАН НА ПОПЫТКЕ ЗАПУСКА КАЗИНО</b>\n\n"
            f"{random.choice(punchlines)}\n\n"
            f"<b>{escape_html(display_name)}</b>, автомат не открылся. "
            "Он просто проверял, кто всё ещё верит в возвращение лудильни.\n\n"
            "Ника: <i>«Записываю тебя в клуб ожидателей казино. "
            "Первое правило клуба: казино не открывается. Второе правило: ты всё равно завтра проверишь.»</i>\n\n"
            "<code>Баланс не тронут. Самоуважение слегка поцарапано.</code>",
            parse_mode="HTML",
        )
        await query.answer("🎰 Джекпот: осознание", show_alert=True)

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
        "tower", "башня", "climb"
    ]

    @router.message(Command(*game_commands))
    async def disabled_games_handler(message: Message) -> None:
        await _games_closed(message)
    
    return router

