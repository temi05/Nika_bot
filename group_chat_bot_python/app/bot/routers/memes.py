from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.admin import is_admin
from app.services.supabase_db import SupabaseDB
from app.utils import escape_html, get_sender_data


def _strip_meme_prefix(value: str) -> str:
    prefixes = ("Мем/контекст чата:", "мем/контекст чата:")
    clean = value.strip()
    for prefix in prefixes:
        if clean.startswith(prefix):
            return clean[len(prefix):].strip()
    return clean


def build_memes_router(db: SupabaseDB) -> Router:
    router = Router(name="memes")

    @router.message(Command("memeadd", "мемдобавить", "мем"))
    async def meme_add_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Мемную базу могут пополнять только администраторы.")
            return

        text = (command.args or "").strip()
        if len(text) < 3:
            await message.answer(
                "Использование: <code>/memeadd что означает локальный мем или фраза</code>",
                parse_mode="HTML",
            )
            return

        if db.add_meme_knowledge(message.chat.id, text, author_id=sender.user_id, author_name=sender.display_name):
            await message.answer("🧠 <b>Мем записан.</b> Теперь Ника сможет учитывать его в ответах.", parse_mode="HTML")
        else:
            await message.answer("❌ Не получилось записать мем.")

    @router.message(Command("memes", "мемы"))
    async def memes_command(message: Message, command: CommandObject) -> None:
        query = (command.args or "").strip()
        rows = db.search_meme_knowledge(message.chat.id, query, limit=10)
        if not rows:
            await message.answer("Пока мемная база пустая." if not query else "По этому запросу мемов не нашла.")
            return

        title = "🧠 <b>Мемная база чата</b>" if not query else f"🧠 <b>Мемы по запросу:</b> <code>{escape_html(query)}</code>"
        lines = [title, ""]
        for index, row in enumerate(rows, start=1):
            lines.append(f"{index}. {escape_html(_strip_meme_prefix(row))}")
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("memedel", "мемудалить"))
    async def meme_delete_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Удалять мемы могут только администраторы.")
            return

        query = (command.args or "").strip()
        if len(query) < 3:
            await message.answer("Использование: <code>/memedel часть текста мема</code>", parse_mode="HTML")
            return

        deleted = db.delete_meme_knowledge(message.chat.id, query)
        if deleted:
            await message.answer(f"🧹 Удалено записей: <b>{deleted}</b>", parse_mode="HTML")
        else:
            await message.answer("Ничего не нашла для удаления.")

    return router