from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.services.supabase_db import SupabaseDB
from app.utils import escape_html, get_sender_data


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in {"creator", "administrator"}


def build_admin_router(bot: Bot, db: SupabaseDB) -> Router:
    router = Router(name="admin")

    @router.message(Command("ban"))
    async def ban_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return

        target_id: int | None = None
        target_name = "пользователя"
        if message.reply_to_message:
            reply_sender = get_sender_data(message.reply_to_message)
            target_id = reply_sender.user_id
            target_name = reply_sender.display_name
        elif command.args and command.args.strip().isdigit():
            target_id = int(command.args.strip())
            target_name = f"ID {target_id}"
        elif command.args:
            found = db.search_user(message.chat.id, command.args.strip())
            if found:
                target_id = found.user_id
                target_name = found.display_name

        if not target_id:
            await message.answer("Используй /ban в реплае или укажи @username / ID.")
            return
        if await is_admin(bot, message.chat.id, target_id):
            await message.answer("Этого пользователя банить нельзя.")
            return

        try:
            await bot.ban_chat_member(message.chat.id, target_id)
            await message.answer(
                f"🚫 <b>{escape_html(sender.display_name)}</b> забанил <b>{escape_html(target_name)}</b>.",
                parse_mode="HTML",
            )
        except Exception:
            await message.answer("Не удалось забанить пользователя.")

    @router.message(Command("unban"))
    async def unban_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return

        target_id: int | None = None
        target_name = "пользователя"
        if command.args and command.args.strip().isdigit():
            target_id = int(command.args.strip())
            target_name = f"ID {target_id}"
        elif command.args:
            found = db.search_user(message.chat.id, command.args.strip())
            if found:
                target_id = found.user_id
                target_name = found.display_name

        if not target_id:
            await message.answer("Используй /unban <id|@username>.")
            return

        try:
            await bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
            await message.answer(
                f"✅ <b>{escape_html(sender.display_name)}</b> разбанил <b>{escape_html(target_name)}</b>.",
                parse_mode="HTML",
            )
        except Exception:
            await message.answer("Не удалось разбанить пользователя.")

    @router.message(Command("banword"))
    async def banword(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            return
        if not command.args:
            await message.answer("Использование: /banword <слово>")
            return
        db.add_bad_word(message.chat.id, command.args.strip().lower())
        await message.answer("Слово добавлено в фильтр.")

    @router.message(Command("unbanword"))
    async def unbanword(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            return
        if not command.args:
            await message.answer("Использование: /unbanword <слово>")
            return
        db.remove_bad_word(message.chat.id, command.args.strip().lower())
        await message.answer("Слово удалено из фильтра.")

    @router.message(Command("listwords"))
    async def listwords(message: Message) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            return
        words = db.get_bad_words(message.chat.id)
        await message.answer(", ".join(words) if words else "Список пуст.")

    return router
