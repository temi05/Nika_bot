from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services.supabase_db import SupabaseDB
from app.utils import escape_html, get_sender_data


# Категории предложений и жалоб
CATEGORIES = {
    "suggestion": {
        "name": "Предложение",
        "emoji": "💡",
        "description": "Новая идея для улучшения бота",
    },
    "bug": {
        "name": "Баг",
        "emoji": "🐛",
        "description": "Ошибка в работе бота",
    },
    "complaint": {
        "name": "Жалоба",
        "emoji": "😤",
        "description": "Несправедливое наказание или действие",
    },
    "feature": {
        "name": "Фичреквест",
        "emoji": "✨",
        "description": "Запрос новой функции",
    },
    "other": {
        "name": "Другое",
        "emoji": "💬",
        "description": "Другой вопрос",
    },
}


def build_feedback_router(db: SupabaseDB) -> Router:
    router = Router(name="feedback")

    @router.message(Command("feedback"))
    async def feedback_help(message: Message, command: CommandObject) -> None:
        """Показать справку о команде feedback"""
        if not command.args:
            lines = [
                "<b>📝 Обратная связь</b>",
                "Предложения и жалобы на работу бота",
                "",
                "<b>Команды:</b>",
                "• /feedback new <категория> <текст> — создать обращение",
                "• /feedback list — мои обращения",
                "• /feedback cancel <id> — отменить обращение",
                "",
                "<b>Категории:</b>",
            ]
            for key, cat in CATEGORIES.items():
                lines.append(f"• {cat['emoji']} <code>{key}</code> — {cat['name']}")
            
            lines.extend([
                "",
                "<b>Примеры:</b>",
                "<code>/feedback new bug Бот не отвечает на /top</code>",
                "<code>/feedback new suggestion Добавить команду обнять</code>",
                "<code>/feedback new complaint Меня забанили без причины</code>",
            ])
            await message.answer("\n".join(lines), parse_mode="HTML")
            return

        # Обработка подкоманд
        args = command.args.strip().split(maxsplit=2)
        subcommand = args[0].lower() if args else ""

        if subcommand == "list":
            await feedback_list(message)
        elif subcommand == "cancel" and len(args) >= 2:
            await feedback_cancel(message, args[1])
        elif subcommand == "new" and len(args) >= 3:
            category = args[1].lower()
            text = args[2]
            await feedback_create(message, category, text)
        else:
            await message.answer(
                "Неверный формат. Пример: /feedback new bug Бот сломался",
                parse_mode="HTML",
            )

    async def feedback_list(message: Message) -> None:
        """Показать список обращений пользователя"""
        sender = get_sender_data(message)
        feedbacks = db.get_user_feedbacks(message.chat.id, sender.user_id)
        
        if not feedbacks:
            await message.answer("📝 У тебя пока нет обращений.")
            return

        lines = ["<b>📝 Твои обращения</b>\n"]
        for fb in feedbacks:
            cat = CATEGORIES.get(fb.category, CATEGORIES["other"])
            status_emoji = "✅" if fb.status == "resolved" else "⏳" if fb.status == "pending" else "🚫"
            lines.append(
                f"{status_emoji} <b>#{fb.id}</b> {cat['emoji']} {cat['name']}\n"
                f"   └ {escape_html(fb.text[:100])}"
            )
            if fb.response:
                lines.append(f"   └ Ответ: {escape_html(fb.response[:100])}")
        
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def feedback_cancel(message: Message, fb_id: str) -> None:
        """Отменить обращение"""
        if not fb_id.isdigit():
            await message.answer("Неверный ID обращения.")
            return

        sender = get_sender_data(message)
        success = db.cancel_feedback(message.chat.id, sender.user_id, int(fb_id))
        
        if success:
            await message.answer(f"✅ Обращение #{fb_id} отменено.")
        else:
            await message.answer(f"❌ Не удалось отменить обращение #{fb_id}.")

    async def feedback_create(message: Message, category: str, text: str) -> None:
        """Создать новое обращение"""
        # Проверка категории
        if category not in CATEGORIES:
            await message.answer(
                f"Неизвестная категория: <code>{category}</code>\n"
                f"Доступные: {', '.join(CATEGORIES.keys())}",
                parse_mode="HTML",
            )
            return

        # Проверка длины текста
        if len(text) < 10:
            await message.answer("Текст обращения слишком короткий (минимум 10 символов).")
            return

        if len(text) > 1000:
            await message.answer("Текст обращения слишком длинный (максимум 1000 символов).")
            return

        sender = get_sender_data(message)
        fb_id = db.create_feedback(
            chat_id=message.chat.id,
            user_id=sender.user_id,
            user_name=sender.display_name,
            category=category,
            text=text,
        )

        if fb_id:
            cat = CATEGORIES[category]
            await message.answer(
                f"✅ Обращение создано!\n\n"
                f"{cat['emoji']} <b>Категория:</b> {cat['name']}\n"
                f"📝 <b>ID:</b> <code>{fb_id}</code>\n"
                f"💬 <b>Текст:</b> {escape_html(text[:200])}...\n\n"
                f"<i>Мы рассмотрим твоё обращение в ближайшее время.</i>",
                parse_mode="HTML",
            )
        else:
            await message.answer("❌ Не удалось создать обращение. Попробуй позже.")

    return router