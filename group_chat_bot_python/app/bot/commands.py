from __future__ import annotations

import random

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.services.ai_service import AIService
from app.services.supabase_db import SupabaseDB
from app.utils import (
    build_progress_bar,
    escape_html,
    get_sender_data,
    human_timedelta,
    parse_birthday_parts,
    parse_simple_reminder_time,
)


def build_commands_router(db: SupabaseDB, bot_name: str, ai: AIService) -> Router:
    router = Router(name="commands")

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        text = (
            "<b>Главное меню бота</b>\n\n"
            "<b>Профиль:</b>\n"
            "/me — профиль\n"
            "/top — топ активных\n"
            "/daily — ежедневный бонус\n"
            "/bio &lt;текст&gt; — обновить био\n"
            "/mybirthday DD.MM[.YYYY] — день рождения\n"
            "/notes [@user] — заметки ИИ\n"
            "/mood — настроение бота\n"
            "/linkfilter [on|off] — фильтр ссылок\n\n"
            "<b>Магазин:</b>\n"
            "/shop — магазин печенек\n"
            "/buy 1|2 — купить уровень или снять варны\n"
            "/give &lt;число&gt; — передать печеньки в реплае\n\n"
            "<b>Развлечения:</b>\n"
            "/casino &lt;сумма&gt; — крутить рулетку (ставка печеньками)\n"
            "/rp &lt;действие&gt; — RP-команды (обнять, поцеловать и др.)\n"
            "/kto &lt;текст&gt; — выбрать, кто...\n"
            "/remind &lt;10m|2h|18:30&gt; &lt;текст&gt; — напоминание\n\n"
            "<b>Обратная связь:</b>\n"
            "/feedback new &lt;категория&gt; &lt;текст&gt; — создать обращение\n"
            "/feedback list — мои обращения\n\n"
            "<b>Админ:</b>\n"
            "/ban, /unban, /banword, /unbanword, /listwords"
        )
        await message.answer(text, parse_mode="HTML")

    @router.message(Command("me"))
    async def me_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        target = db.get_or_create_user(message.chat.id, sender)

        if message.reply_to_message:
            target = db.get_or_create_user(message.chat.id, get_sender_data(message.reply_to_message))
        elif command.args:
            found = db.search_user(message.chat.id, command.args)
            if found:
                target = found

        next_xp = db.get_next_level_xp(target.level)
        prev_xp = 0 if target.level <= 1 else db.get_next_level_xp(target.level - 1)
        progress = ((target.xp - prev_xp) / max(next_xp - prev_xp, 1)) * 100
        rank = _get_rank_name(target.level)
        body = (
            f"<b>{'Мой профиль' if target.user_id == sender.user_id else 'Профиль пользователя'}</b>\n\n"
            f"Имя: <code>{escape_html(target.display_name)}</code>\n"
            f"Ранг: <b>{escape_html(rank)}</b>\n"
            f"Роль: <i>Пользователь</i>\n"
            f"Уровень: <b>{target.level}</b>\n"
            f"Прогресс: <code>{build_progress_bar(progress)} {int(progress)}%</code>\n"
            f"XP: <code>{target.xp} / {next_xp}</code>\n"
            f"Печеньки: <code>{target.reputation}</code>\n"
        )
        if target.warns > 0:
            body += f"Предупреждения: <code>{target.warns} / {db.settings.warn_limit}</code>\n"
        if target.birthday:
            body += f"День рождения: <code>{escape_html(target.birthday)}</code>\n"
        if target.bio:
            body += f"О себе: <i>{escape_html(target.bio)}</i>\n"
        body += f"\n<i>До следующего уровня осталось {max(next_xp - target.xp, 0)} XP</i>"
        await message.answer(body, parse_mode="HTML")

    @router.message(Command("top"))
    async def top_command(message: Message) -> None:
        users = db.get_top_users(message.chat.id, limit=10)
        if not users:
            await message.answer("В этом чате пока нет активных участников.")
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = ["<b>Топ-10 активных участников</b>", ""]
        for idx, user in enumerate(users, start=1):
            prefix = medals[idx - 1] if idx <= 3 else f"{idx}."
            warns_marker = " ⚠️" if user.warns > 0 else ""
            lines.append(
                f"{prefix} <code>{escape_html(user.display_name)}</code>\n"
                f"   └ <b>Ур. {user.level}</b> | 🍪 {user.reputation}{warns_marker}"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("daily"))
    async def daily_command(message: Message) -> None:
        sender = get_sender_data(message)
        user = db.get_or_create_user(message.chat.id, sender)
        result = db.claim_daily_bonus(user)
        if not result["success"]:
            await message.answer(
                f"⏳ <b>{escape_html(sender.display_name)}</b>, следующий бонус будет доступен через "
                f"<code>{human_timedelta(result['hours'], result['minutes'])}</code>.",
                parse_mode="HTML",
            )
            return

        lines = [
            "🎁 <b>Ежедневный бонус</b>",
            "",
            f"✨ XP: <b>+{result['bonus_xp']}</b>",
        ]
        if result["is_rep_gained"]:
            lines.append("🍪 Бонусом выпала ещё и 1 печенька.")
        if result["level_up"]:
            lines.append(f"🎉 Новый уровень: <b>{result['new_level']}</b>")
        lines.append(f"🍪 Печеньки: <code>{result['new_reputation']}</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("bio"))
    async def bio_command(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /bio &lt;текст&gt;", parse_mode="HTML")
            return
        sender = get_sender_data(message)
        user = db.get_or_create_user(message.chat.id, sender)
        db.set_bio(user, command.args.strip())
        await message.answer("Био обновлено.")

    @router.message(Command("mybirthday"))
    async def birthday_command(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /mybirthday DD.MM или DD.MM.YYYY")
            return
        if not parse_birthday_parts(command.args.strip()):
            await message.answer("Формат даты: DD.MM или DD.MM.YYYY")
            return
        sender = get_sender_data(message)
        user = db.get_or_create_user(message.chat.id, sender)
        db.set_birthday(user, command.args.strip())
        await message.answer("День рождения сохранён.")

    @router.message(Command("notes"))
    async def notes_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        target = db.get_or_create_user(message.chat.id, sender)
        if message.reply_to_message:
            target = db.get_or_create_user(message.chat.id, get_sender_data(message.reply_to_message))
        elif command.args:
            found = db.search_user(message.chat.id, command.args)
            if found:
                target = found

        notes = target.ai_notes or "Пока заметок нет."
        facts = await ai.memory.get_relevant_facts(message.chat.id, target.display_name, target.display_name)
        if facts:
            notes = f"{notes}\n\nПамять:\n{facts}"
        await message.answer(
            f"<b>Заметки ИИ про {escape_html(target.display_name)}</b>\n\n<i>{escape_html(notes)}</i>",
            parse_mode="HTML",
        )

    @router.message(Command("mood"))
    async def mood_command(message: Message) -> None:
        mood = ai.moods[message.chat.id]
        status = "Нормальное"
        emoji = "💠"
        if mood >= 90:
            status, emoji = "В восторге", "🤩"
        elif mood >= 75:
            status, emoji = "Хорошее", "😊"
        elif mood <= 20:
            status, emoji = "В ярости", "🤬"
        elif mood <= 35:
            status, emoji = "Раздражена", "😒"
        await message.answer(
            f"{emoji} <b>Настроение {escape_html(bot_name)}</b>\n"
            f"Статус: <b>{escape_html(status)}</b>\n"
            f"Уровень: <code>{build_progress_bar(mood)} {mood}%</code>",
            parse_mode="HTML",
        )

    @router.message(Command("linkfilter"))
    async def linkfilter_command(message: Message, command: CommandObject) -> None:
        settings = db.get_chat_settings(message.chat.id)
        if not command.args:
            status = "включён" if settings.link_filter_enabled else "выключен"
            await message.answer(f"Фильтр ссылок сейчас {status}.")
            return
        enabled = command.args.strip().lower() == "on"
        db.update_chat_settings(message.chat.id, link_filter_enabled=enabled)
        await message.answer(f"Фильтр ссылок {'включён' if enabled else 'выключен'}.")

    @router.message(Command("shop"))
    async def shop_command(message: Message) -> None:
        await message.answer(
            "<b>Магазин печенек</b>\n\n"
            "1. Купить 1 уровень — <code>500 🍪</code>\n"
            "   Команда: <code>/buy 1</code>\n\n"
            "2. Снять все предупреждения — <code>200 🍪</code>\n"
            "   Команда: <code>/buy 2</code>\n\n"
            "<i>Печеньки зарабатываются за спасибо, плюсики и активность.</i>",
            parse_mode="HTML",
        )

    @router.message(Command("buy"))
    async def buy_command(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Использование: /buy 1 или /buy 2")
            return
        sender = get_sender_data(message)
        user = db.get_or_create_user(message.chat.id, sender)
        ok, result = db.purchase_item(user, int(command.args.strip()))
        await message.answer(("✅ " if ok else "❌ ") + result)

    @router.message(Command("give"))
    async def give_command(message: Message, command: CommandObject) -> None:
        if not message.reply_to_message or not command.args or not command.args.strip().isdigit():
            await message.answer("Использование: /give &lt;число&gt; в ответ на сообщение.", parse_mode="HTML")
            return
        amount = int(command.args.strip())
        if amount <= 0:
            await message.answer("Сумма должна быть больше нуля.")
            return

        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        receiver = db.get_or_create_user(message.chat.id, get_sender_data(message.reply_to_message))
        if not db.transfer_cookies(sender, receiver, amount):
            await message.answer("Не удалось передать печеньки.")
            return

        # Генерируем умное сообщение с помощью AI
        gift_message = await ai.generate_cookie_gift_message(
            message.chat.id,
            sender.display_name,
            receiver.display_name,
            amount,
        )
        await message.answer(gift_message, parse_mode="HTML")

    @router.message(Command("casino", "gamble", "spin", "казино", "ставка"))
    async def casino_command(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Использование: /casino &lt;сумма&gt;", parse_mode="HTML")
            return
            
        bet = int(command.args.strip())
        if bet <= 0:
            await message.answer("Ставка должна быть больше нуля.")
            return

        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if sender.reputation < bet:
            await message.answer(f"Недостаточно печенек! У тебя всего {sender.reputation} 🍪.")
            return

        roll = random.randint(1, 100)
        
        if roll <= 50:
            db.update_user(sender.id, {"reputation": sender.reputation - bet})
            await message.answer(f"🎰 Автомат показал 🍒🍋🍉\n\nУвы, ты проиграл {bet} 🍪. Осталось: {sender.reputation - bet} 🍪.")
        elif roll <= 80:
            win = int(bet * 1.5)
            db.update_user(sender.id, {"reputation": sender.reputation - bet + win})
            await message.answer(f"🎰 Автомат показал 🍒🍒🍇\n\nТы выиграл {win} 🍪! Текущий баланс: {sender.reputation - bet + win} 🍪.")
        elif roll <= 95:
            win = bet * 2
            db.update_user(sender.id, {"reputation": sender.reputation - bet + win})
            await message.answer(f"🎰 Автомат показал 🍉🍉🍉\n\nУдача! Ты выиграл {win} 🍪! Текущий баланс: {sender.reputation - bet + win} 🍪.")
        else:
            win = bet * 5
            db.update_user(sender.id, {"reputation": sender.reputation - bet + win})
            await message.answer(f"🎰 Автомат показал 💎💎💎\n\nДЖЕКПОТ! Ты выиграл {win} 🍪! Текущий баланс: {sender.reputation - bet + win} 🍪.")

    @router.message(Command("kto"))
    async def kto_command(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /kto &lt;текст&gt;", parse_mode="HTML")
            return
        allowed, remaining = db.can_use_command(message.chat.id, "kto", 60)
        if not allowed:
            await message.answer(
                f"⏳ Команда /kto на перезарядке. Подожди ещё <code>{remaining} сек.</code>.",
                parse_mode="HTML",
            )
            return

        users = db.get_all_users(message.chat.id)
        if not users:
            await message.answer("Некого выбирать.")
            return
        selected = random.choice(users)
        await message.answer(
            f"🤔 Я думаю, что <b>{escape_html(command.args)}</b> — это "
            f"<b>{escape_html(selected.display_name)}</b>!",
            parse_mode="HTML",
        )

    @router.message(Command("remind"))
    async def remind_command(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /remind &lt;10m|2h|18:30&gt; &lt;текст&gt;", parse_mode="HTML")
            return
        raw = command.args.strip().split(maxsplit=1)
        if len(raw) != 2:
            await message.answer("Использование: /remind &lt;10m|2h|18:30&gt; &lt;текст&gt;", parse_mode="HTML")
            return
        trigger = parse_simple_reminder_time(raw[0])
        if not trigger:
            await message.answer("Не понял время. Примеры: 10m, 2h, 18:30")
            return
        sender = get_sender_data(message)
        reminder = db.insert_reminder(message.chat.id, sender.user_id, sender.display_name, raw[1], trigger)
        if not reminder:
            await message.answer("Не удалось создать напоминание.")
            return
        await message.answer(
            f"⏰ Напоминание сохранено на <code>{trigger.astimezone().strftime('%d.%m %H:%M')}</code>.",
            parse_mode="HTML",
        )

    return router


def _get_rank_name(level: int) -> str:
    if level >= 50:
        return "Легенда"
    if level >= 30:
        return "Элита"
    if level >= 20:
        return "Мастер"
    if level >= 10:
        return "Опытный"
    if level >= 5:
        return "Активный"
    return "Новичок"
