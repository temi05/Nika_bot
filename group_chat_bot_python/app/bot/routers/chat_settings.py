"""
Роутер настроек чата (/chatsettings).
"""
from __future__ import annotations

from typing import Any

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.admin import is_admin
from app.services.supabase_db import SupabaseDB
from app.utils import get_sender_data

_settings_cache: dict[int, dict[str, Any]] = {}


def _invalidate(chat_id: int) -> None:
    _settings_cache.pop(chat_id, None)


def _get_cached(db: SupabaseDB, chat_id: int) -> dict[str, Any]:
    if chat_id not in _settings_cache:
        s = db.get_chat_settings(chat_id)
        _settings_cache[chat_id] = {
            "link_filter_enabled": s.link_filter_enabled,
            "casino_jackpot": s.casino_jackpot,
            "auto_drop_enabled": s.auto_drop_enabled,
            "auto_quiz_enabled": s.auto_quiz_enabled,
            "ai_enabled": s.ai_enabled,
            "proactive_enabled": s.proactive_enabled,
        }
    return _settings_cache[chat_id]


get_chat_settings_cached = _get_cached
invalidate_chat_settings_cache = _invalidate


def _bool_icon(value: bool) -> str:
    return "✅" if value else "❌"


def _status(value: bool) -> str:
    return "Вкл" if value else "Выкл"


def _build_settings_text(settings: dict[str, Any], chat_title: str | None) -> str:
    title = "⚙️ <b>Настройки чата</b>"
    if chat_title:
        title += f" «{chat_title}»"
    lines = [
        title,
        "",
        f"🔗 Фильтр ссылок: <b>{_status(settings['link_filter_enabled'])}</b>",
        f"🍪 Авто-дропы: <b>{_status(settings['auto_drop_enabled'])}</b>",
        f"🧠 Авто-викторины: <b>{_status(settings['auto_quiz_enabled'])}</b>",
        f"🤖 ИИ-функции: <b>{_status(settings['ai_enabled'])}</b>",
        f"💬 Самостоятельные реплики: <b>{_status(settings['proactive_enabled'])}</b>",
        f"🎰 Джекпот казино: <b>{settings['casino_jackpot']} 🍪</b>",
        "",
        "<i>Менять настройки могут только администраторы.</i>",
    ]
    return "\n".join(lines)


def _build_settings_kb(settings: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{_bool_icon(settings['link_filter_enabled'])} Фильтр ссылок",
                callback_data="cs_toggle_linkfilter",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{_bool_icon(settings['auto_drop_enabled'])} Авто-дропы",
                callback_data="cs_toggle_auto_drop",
            ),
            InlineKeyboardButton(
                text=f"{_bool_icon(settings['auto_quiz_enabled'])} Авто-викторины",
                callback_data="cs_toggle_auto_quiz",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{_bool_icon(settings['ai_enabled'])} ИИ-функции",
                callback_data="cs_toggle_ai",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{_bool_icon(settings['proactive_enabled'])} Самостоятельные реплики",
                callback_data="cs_toggle_proactive",
            ),
        ],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="cs_refresh")],
    ])


def build_chat_settings_router(db: SupabaseDB) -> Router:
    router = Router(name="chat_settings")

    @router.message(Command("chatsettings", "настройки", "settings"))
    async def chatsettings_command(message: Message) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Настройки могут смотреть и менять только администраторы.")
            return
        settings = _get_cached(db, message.chat.id)
        await message.answer(
            _build_settings_text(settings, message.chat.title or None),
            reply_markup=_build_settings_kb(settings),
            parse_mode="HTML",
        )

    async def _require_admin_cb(query: CallbackQuery) -> bool:
        user_id = query.from_user.id if query.from_user else 0
        if not query.message or not await is_admin(query.bot, query.message.chat.id, user_id):
            await query.answer("❌ Только для администраторов.", show_alert=True)
            return False
        return True

    async def _toggle_setting(query: CallbackQuery, key: str, label: str) -> None:
        if not await _require_admin_cb(query):
            return
        chat_id = query.message.chat.id
        current = _get_cached(db, chat_id)
        new_val = not bool(current[key])
        if not db.update_chat_settings(chat_id, **{key: new_val}):
            await query.answer("Не получилось сохранить. Проверь SQL-миграцию chats.", show_alert=True)
            return
        _invalidate(chat_id)
        settings = _get_cached(db, chat_id)
        try:
            await query.message.edit_text(
                _build_settings_text(settings, query.message.chat.title or None),
                reply_markup=_build_settings_kb(settings),
                parse_mode="HTML",
            )
        except Exception:
            pass
        status = "включено" if new_val else "выключено"
        await query.answer(f"{label}: {status}.")

    @router.callback_query(F.data == "cs_toggle_linkfilter")
    async def toggle_linkfilter(query: CallbackQuery) -> None:
        await _toggle_setting(query, "link_filter_enabled", "Фильтр ссылок")

    @router.callback_query(F.data == "cs_toggle_auto_drop")
    async def toggle_auto_drop(query: CallbackQuery) -> None:
        await _toggle_setting(query, "auto_drop_enabled", "Авто-дропы")

    @router.callback_query(F.data == "cs_toggle_auto_quiz")
    async def toggle_auto_quiz(query: CallbackQuery) -> None:
        await _toggle_setting(query, "auto_quiz_enabled", "Авто-викторины")

    @router.callback_query(F.data == "cs_toggle_ai")
    async def toggle_ai(query: CallbackQuery) -> None:
        await _toggle_setting(query, "ai_enabled", "ИИ-функции")

    @router.callback_query(F.data == "cs_toggle_proactive")
    async def toggle_proactive(query: CallbackQuery) -> None:
        await _toggle_setting(query, "proactive_enabled", "Самостоятельные реплики")

    @router.callback_query(F.data == "cs_refresh")
    async def refresh_settings(query: CallbackQuery) -> None:
        if not query.message:
            return
        chat_id = query.message.chat.id
        _invalidate(chat_id)
        settings = _get_cached(db, chat_id)
        try:
            await query.message.edit_text(
                _build_settings_text(settings, query.message.chat.title or None),
                reply_markup=_build_settings_kb(settings),
                parse_mode="HTML",
            )
        except Exception:
            pass
        await query.answer("Обновлено.")

    return router
