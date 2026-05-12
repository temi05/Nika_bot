"""
Роутер: настройки чата (/chatsettings).
Интерактивное меню через InlineKeyboard с кэшем ChatSettings в памяти.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.admin import is_admin
from app.services.supabase_db import SupabaseDB
from app.utils import get_sender_data

# ─────────── Простейший in-process кэш ───────────
# Хранит ChatSettings в словаре; при update() – инвалидирует.
_settings_cache: dict[int, dict[str, Any]] = {}


def _invalidate(chat_id: int) -> None:
    _settings_cache.pop(chat_id, None)


def _get_cached(db: SupabaseDB, chat_id: int) -> Any:
    """Возвращает ChatSettings из кэша или грузит из БД."""
    if chat_id not in _settings_cache:
        s = db.get_chat_settings(chat_id)
        _settings_cache[chat_id] = {
            "link_filter_enabled": s.link_filter_enabled,
            "casino_jackpot": s.casino_jackpot,
        }
    return _settings_cache[chat_id]


# Публичные алиасы для других модулей
get_chat_settings_cached = _get_cached
invalidate_chat_settings_cache = _invalidate


# ─────────── Построение меню ───────────

def _bool_icon(value: bool) -> str:
    return "✅" if value else "❌"


def _build_settings_text(settings: dict, chat_title: str | None) -> str:
    title = f"⚙️ <b>Настройки чата</b>"
    if chat_title:
        title += f" «{chat_title}»"
    lines = [
        title,
        "",
        f"🔗 Фильтр ссылок: <b>{'Вкл' if settings['link_filter_enabled'] else 'Выкл'}</b>",
        f"🎰 Джекпот казино: <b>{settings['casino_jackpot']} 🍪</b>",
        "",
        "<i>Нажми кнопку ниже, чтобы изменить настройку.\nМеняют только администраторы.</i>",
    ]
    return "\n".join(lines)


def _build_settings_kb(settings: dict) -> InlineKeyboardMarkup:
    lf = settings["link_filter_enabled"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{_bool_icon(lf)} Фильтр ссылок",
                callback_data="cs_toggle_linkfilter"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data="cs_refresh"
            ),
        ],
    ])


# ─────────── Роутер ───────────

def build_chat_settings_router(db: SupabaseDB) -> Router:
    router = Router(name="chat_settings")

    @router.message(Command("chatsettings", "настройки", "settings"))
    async def chatsettings_command(message: Message) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Настройки могут смотреть и менять только администраторы.")
            return
        settings = _get_cached(db, message.chat.id)
        chat_title = message.chat.title or None
        await message.answer(
            _build_settings_text(settings, chat_title),
            reply_markup=_build_settings_kb(settings),
            parse_mode="HTML",
        )

    async def _require_admin_cb(query: CallbackQuery) -> bool:
        user_id = query.from_user.id if query.from_user else 0
        if not await is_admin(query.bot, query.message.chat.id, user_id):
            await query.answer("❌ Только для администраторов.", show_alert=True)
            return False
        return True

    @router.callback_query(F.data == "cs_toggle_linkfilter")
    async def toggle_linkfilter(query: CallbackQuery) -> None:
        if not await _require_admin_cb(query):
            return
        chat_id = query.message.chat.id
        current = _get_cached(db, chat_id)
        new_val = not current["link_filter_enabled"]
        db.update_chat_settings(chat_id, link_filter_enabled=new_val)
        _invalidate(chat_id)  # сбросить кэш после обновления
        settings = _get_cached(db, chat_id)
        chat_title = query.message.chat.title or None
        try:
            await query.message.edit_text(
                _build_settings_text(settings, chat_title),
                reply_markup=_build_settings_kb(settings),
                parse_mode="HTML",
            )
        except Exception:
            pass
        status = "включён" if new_val else "выключен"
        await query.answer(f"Фильтр ссылок теперь {status}.")

    @router.callback_query(F.data == "cs_refresh")
    async def refresh_settings(query: CallbackQuery) -> None:
        chat_id = query.message.chat.id
        _invalidate(chat_id)
        settings = _get_cached(db, chat_id)
        chat_title = query.message.chat.title or None
        try:
            await query.message.edit_text(
                _build_settings_text(settings, chat_title),
                reply_markup=_build_settings_kb(settings),
                parse_mode="HTML",
            )
        except Exception:
            pass
        await query.answer("Обновлено!")

    return router
