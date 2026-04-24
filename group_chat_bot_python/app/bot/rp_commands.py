from __future__ import annotations

import random

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.services.supabase_db import SupabaseDB
from app.utils import escape_html, get_sender_data


# Словарь RP-команд с их описанием и стоимостью
RP_ACTIONS = {
    # Базовые действия (бесплатно или дешево)
    "обнять": {
        "emoji": "🤗",
        "cost": 0,
        "self": "Обняла себя сама 🤗",
        "template": "{sender} обнял(а) {target} 🤗",
    },
    "погладить": {
        "emoji": "👋",
        "cost": 0,
        "self": "Погладила себя по головке 👋",
        "template": "{sender} погладил(а) {target} по головке 👋",
    },
    "поцеловать": {
        "emoji": "💋",
        "cost": 1,
        "self": "Поцеловала себя в зеркало 💋",
        "template": "{sender} поцеловал(а) {target} 💋",
    },
    "пожать_руку": {
        "emoji": "🤝",
        "cost": 0,
        "self": "Пожала свою руку... странно 🤔",
        "template": "{sender} пожал(а) руку {target} 🤝",
    },
    "взять_за_руку": {
        "emoji": "👫",
        "cost": 0,
        "self": "Взяла себя за руку... странно 🤔",
        "template": "{sender} взял(а) {target} за руку 👫",
    },
    "похлопать": {
        "emoji": "👏",
        "cost": 0,
        "self": "Похлопала в ладоши себе 👏",
        "template": "{sender} похлопал(а) {target} по плечу 👏",
    },
    
    # Эмоциональные действия
    "поддержать": {
        "emoji": "💪",
        "cost": 0,
        "self": "Поддержала себя морально 💪",
        "template": "{sender} поддержал(а) {target} 💪",
    },
    "утешить": {
        "emoji": "😢",
        "cost": 0,
        "self": "Утешила себя... не помогло 😢",
        "template": "{sender} утешил(а) {target} 😢",
    },
    "порадоваться": {
        "emoji": "🎉",
        "cost": 0,
        "self": "Порадовалась за себя 🎉",
        "template": "{sender} порадовался(ась) за {target} 🎉",
    },
    "поздравить": {
        "emoji": "🎊",
        "cost": 0,
        "self": "Поздравила себя 🎊",
        "template": "{sender} поздравил(а) {target} 🎊",
    },
    
    # Игривые действия
    "пощекотать": {
        "emoji": "😆",
        "cost": 0,
        "self": "Пощекотала себя... не смешно 😆",
        "template": "{sender} пощекотал(а) {target} 😆",
    },
    "подразнить": {
        "emoji": "😏",
        "cost": 0,
        "self": "Подразнила своё отражение 😏",
        "template": "{sender} подразнил(а) {target} 😏",
    },
    "шлепнуть": {
        "emoji": "😵",
        "cost": 2,
        "self": "Шлёпнула себя... больно 😵",
        "template": "{sender} шлёпнул(а) {target} 😵",
    },
    "укусить": {
        "emoji": "🦷",
        "cost": 2,
        "self": "Укусила себя... невкусно 🦷",
        "template": "{sender} укусил(а) {target} 🦷",
    },
    "лизнуть": {
        "emoji": "👅",
        "cost": 3,
        "self": "Лизнула себя... странный вкус 👅",
        "template": "{sender} лизнул(а) {target} 👅",
    },
    
    # Интимные действия (только для взрослых чатов)
    "облизать": {
        "emoji": "😽",
        "cost": 5,
        "self": "Облизала себя... фу 😐",
        "template": "{sender} облизал(а) {target} 😽",
    },
    "прижать": {
        "emoji": "💕",
        "cost": 2,
        "self": "Прижалась к стенке 💕",
        "template": "{sender} прижал(а) {target} к себе 💕",
    },
    "шепнуть": {
        "emoji": "🤫",
        "cost": 1,
        "self": "Шепнула себе... глупо 🤫",
        "template": "{sender} шепнул(а) {target} на ушко: «{phrase}» 🤫",
    },
}


def build_rp_router(db: SupabaseDB) -> Router:
    router = Router(name="rp")

    @router.message(Command("rp"))
    async def rp_handler(message: Message, command: CommandObject) -> None:
        """Обработчик команды /rp - показывает справку или выполняет действие"""
        # Если нет аргументов - показываем справку
        if not command.args:
            lines = [
                "<b>🎭 RP-команды</b>\n",
                "<i>Использование: /rp &lt;действие&gt; в ответ на сообщение</i>\n",
                "<b>Базовые:</b>",
                "• обнять, погладить, поцеловать",
                "• пожать_руку, взять_за_руку, похлопать",
                "",
                "<b>Эмоциональные:</b>",
                "• поддержать, утешить, порадоваться, поздравить",
                "",
                "<b>Игривые:</b>",
                "• пощекотать, подразнить, шлепнуть, укусить, лизнуть",
                "",
                "<b>Особые:</b>",
                "• прижать, облизать, шепнуть &lt;текст&gt;",
                "",
                "<i>Некоторые действия требуют печенек.</i>",
            ]
            await message.answer("\n".join(lines), parse_mode="HTML")
            return

        # Проверяем, есть ли reply на сообщение
        if not message.reply_to_message:
            await message.answer("Использование: /rp &lt;действие&gt; в ответ на сообщение пользователя")
            return

        # Парсим команду и текст (для шепнуть)
        parts = command.args.strip().split(maxsplit=1)
        action_name = parts[0].lower()
        extra_phrase = parts[1] if len(parts) > 1 else None

        # Проверяем существование действия
        if action_name not in RP_ACTIONS:
            await message.answer(
                f"Неизвестное действие: <code>{action_name}</code>\n"
                f"Напиши /rp для списка команд.",
                parse_mode="HTML",
            )
            return

        action = RP_ACTIONS[action_name]
        sender = get_sender_data(message)
        target = get_sender_data(message.reply_to_message)

        # Проверка на самого себя
        if sender.user_id == target.user_id:
            await message.answer(action["self"])
            return

        # Проверка стоимости
        cost = action.get("cost", 0)
        if cost > 0:
            sender_user = db.get_or_create_user(message.chat.id, sender)
            if sender_user.reputation < cost:
                await message.answer(
                    f"😔 Нужно {cost} печенек для этого действия. "
                    f"У тебя: {sender_user.reputation} 🍪",
                    parse_mode="HTML",
                )
                return
            # Списываем печеньки
            db.add_reputation(sender_user, -cost)

        # Формируем результат
        if action_name == "шепнуть" and extra_phrase:
            result = action["template"].format(
                sender=escape_html(sender.display_name),
                target=escape_html(target.display_name),
                phrase=escape_html(extra_phrase),
            )
        else:
            result = action["template"].format(
                sender=escape_html(sender.display_name),
                target=escape_html(target.display_name),
            )

        await message.answer(result, parse_mode="HTML")

    return router