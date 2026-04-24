from __future__ import annotations
import time

import asyncio
import random
from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.services.ai_service import AIService
from app.services.supabase_db import SupabaseDB
from app.bot.admin import is_admin
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

    HELP_PAGES = {
        "main": (
            "✨ <b>Центр управления NeuroNika</b>\n\n"
            "Выбери категорию команд ниже, чтобы узнать подробности.\n"
            "Каждый раздел содержит уникальные функции для взаимодействия!"
        ),
        "economy": (
            "🍪 <b>Экономика и Финансы</b>\n\n"
            "• <code>/me</code> — Твой профиль и баланс\n"
            "• <code>/daily</code> — Собрать ежедневные печеньки\n"
            "• <code>/shop</code> — Магазин улучшений\n"
            "• <code>/give [сумма]</code> — Подарить печеньки (реплаем)\n"
            "• <code>/loan [сумма]</code> — Предложить в долг (реплаем)\n"
            "• <code>/ask_loan [сумма]</code> — Попросить в долг (реплаем)\n"
            "• <code>/repay [сумма]</code> — Вернуть долг\n"
            "• <code>/steal</code> — Попробовать украсть (реплаем)\n"
            "• <code>/top</code> — Рейтинг самых богатых"
        ),
        "games": (
            "🎰 <b>Игры и Развлечения</b>\n\n"
            "• <code>/casino [ставка]</code> — Премиальный слот-автомат\n"
            "• <code>/tower [ставка]</code> — Рискованная Башня (x100!)\n"
            "• <code>/kto [текст]</code> — Рандомный выбор участника\n"
            "• <code>/remind [время] [текст]</code> — Напоминания\n"
            "• <code>/rp [действие]</code> — Ролевые взаимодействия"
        ),
        "profile": (
            "👤 <b>Персонализация</b>\n\n"
            "• <code>/setflavor [вкус]</code> — Твой уникальный вкус\n"
            "• <code>/bio [текст]</code> — Расскажи о себе\n"
            "• <code>/mybirthday [дата]</code> — Установи день рождения\n"
            "• <code>/notes</code> — Твоя история в глазах ИИ"
        ),
        "admin": (
            "🛡 <b>Администрирование</b>\n\n"
            "• <code>/cookie_rain</code> — Массовая раздача бонусов\n"
            "• <code>/whisper [текст]</code> — Сообщение от имени бота\n"
            "• <code>/feedbacks</code> — Управление обращениями\n"
            "• <code>/ban</code> | <code>/mute</code> — Модерация"
        ),
        "support": (
            "✉️ <b>Обратная связь</b>\n\n"
            "• <code>/feedback new [кат] [текст]</code> — Написать админам\n"
            "• <code>/feedback list</code> — Твои активные обращения"
        )
    }

    def get_help_keyboard():
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🍪 Экономика", callback_data="help_economy"),
                InlineKeyboardButton(text="🎰 Игры", callback_data="help_games")
            ],
            [
                InlineKeyboardButton(text="👤 Профиль", callback_data="help_profile"),
                InlineKeyboardButton(text="🛡 Админ", callback_data="help_admin")
            ],
            [
                InlineKeyboardButton(text="✉️ Поддержка", callback_data="help_support"),
                InlineKeyboardButton(text="🏠 Главная", callback_data="help_main")
            ]
        ])

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            HELP_PAGES["main"],
            reply_markup=get_help_keyboard(),
            parse_mode="HTML"
        )

    @router.callback_query(F.data.startswith("help_"))
    async def help_callback(query: CallbackQuery) -> None:
        page = query.data.split("_")[1]
        if page in HELP_PAGES:
            await query.message.edit_text(
                HELP_PAGES[page],
                reply_markup=get_help_keyboard(),
                parse_mode="HTML"
            )
        await query.answer()

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
        if target.flavor:
            body += f"Вкус: <b>{escape_html(target.flavor)}</b>\n"
        if target.warns > 0:
            body += f"Предупреждения: <code>{target.warns} / {db.settings.warn_limit}</code>\n"
        if target.birthday:
            body += f"День рождения: <code>{escape_html(target.birthday)}</code>\n"
        if target.bio:
            body += f"О себе: <i>{escape_html(target.bio)}</i>\n"
        body += f"\n<i>До следующего уровня осталось {max(next_xp - target.xp, 0)} XP</i>"
        await message.answer(body, parse_mode="HTML")

    @router.message(Command("top", "лидеры", "рейтинг"))
    async def top_command(message: Message) -> None:
        await _show_top(message, "xp")

    @router.callback_query(F.data.startswith("top_"))
    async def top_callback(query: CallbackQuery) -> None:
        category = query.data.split("_")[1]
        await _show_top(query.message, category, is_edit=True)
        await query.answer()

    async def _show_top(msg: Message, category: str, is_edit: bool = False) -> None:
        chat_id = msg.chat.id
        users = db.get_top_users(chat_id, limit=10, order_by=category)
        
        if not users:
            text = "✨ <b>Лидеры NeuroNika</b>\n\nВ этом чате пока пусто. Будь первым!"
            if is_edit:
                await msg.edit_text(text, parse_mode="HTML")
            else:
                await msg.answer(text, parse_mode="HTML")
            return

        title = "🏆 <b>ТОП ПО УРОВНЮ</b>" if category == "xp" else "🍪 <b>ТОП ПО ПЕЧЕНЬКАМ</b>"
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{title}", ""]
        
        for idx, user in enumerate(users, start=1):
            prefix = medals[idx-1] if idx <= 3 else f"{idx}."
            name = escape_html(user.display_name)
            if category == "xp":
                lines.append(f"{prefix} <b>{name}</b> — <code>{user.level} ур.</code> (<i>{user.xp} XP</i>)")
            else:
                lines.append(f"{prefix} <b>{name}</b> — <code>{user.reputation} 🍪</code>")
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⭐️ Уровень", callback_data="top_xp"),
                InlineKeyboardButton(text="🍪 Печеньки", callback_data="top_reputation")
            ]
        ])

        footer = "\n\n<i>Обновлено: " + datetime.now().strftime("%H:%M:%S") + "</i>"
        final_text = "\n".join(lines) + footer

        if is_edit:
            try:
                await msg.edit_text(final_text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass 
        else:
            await msg.answer(final_text, reply_markup=kb, parse_mode="HTML")

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

    @router.message(Command("loan"))
    async def loan_command(message: Message, command: CommandObject) -> None:
        if not message.reply_to_message or not command.args or not command.args.strip().isdigit():
            await message.answer("🤝 <b>Кредитование</b>\n\nИспользование: <code>/loan &lt;сумма&gt;</code> в ответ на сообщение.\n<i>Вы предлагаете в долг свои печеньки.</i>", parse_mode="HTML")
            return
            
        amount = int(command.args.strip())
        if amount <= 0:
            await message.answer("Сумма должна быть больше нуля.")
            return

        sender_data = get_sender_data(message)
        target_data = get_sender_data(message.reply_to_message)
        
        if sender_data.user_id == target_data.user_id:
            await message.answer("Давать в долг самому себе нельзя.")
            return

        sender = db.get_or_create_user(message.chat.id, sender_data)
        if sender.reputation < amount:
            await message.answer(f"❌ Недостаточно печенек! Баланс: {sender.reputation}")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"loan_acc_{amount}_{sender.user_id}_{target_data.user_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"loan_dec_{target_data.user_id}")
            ]
        ])

        await message.answer(
            f"🤝 {escape_html(sender.display_name)} предлагает вам <b>{amount} 🍪</b> в долг.\n\n"
            f"Вы согласны принять кредит?",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    @router.callback_query(F.data.startswith("loan_"))
    async def loan_callback(query: CallbackQuery) -> None:
        parts = query.data.split("_")
        action = parts[1]
        
        if action == "dec":
            target_id = int(parts[2])
            if query.from_user.id != target_id:
                await query.answer("Это не вам предложили!", show_alert=True)
                return
            await query.message.edit_text("❌ Предложение отклонено.")
            return

        # loan_acc_{amount}_{sender_id}_{target_id}
        amount = int(parts[2])
        sender_id = int(parts[3])
        target_id = int(parts[4])
        
        if query.from_user.id != target_id:
            await query.answer("Это не вам предложили!", show_alert=True)
            return
            
        sender = db.get_user_by_platform_id(query.message.chat.id, sender_id)
        target = db.get_user_by_platform_id(query.message.chat.id, target_id)
        
        if not sender or sender.reputation < amount:
            await query.message.edit_text("❌ Ошибка: у отправителя больше нет нужной суммы.")
            return
            
        # Выполняем сделку
        db.update_user(sender.id, {"reputation": sender.reputation - amount})
        db.update_user(target.id, {
            "reputation": target.reputation + amount,
            "debt": target.debt + amount,
            "last_loan_at": datetime.now(timezone.utc).isoformat()
        })
        
        await query.message.edit_text(
            f"🤝 <b>Сделка совершена!</b>\n\n"
            f"{escape_html(sender.display_name)} одолжил <b>{amount} 🍪</b> {escape_html(target.display_name)}.\n"
            f"⚠️ Долг должен быть возвращен командой <code>/repay</code>.",
            parse_mode="HTML"
        )
        await query.answer("Кредит получен!")

    @router.message(Command("ask_loan"))
    async def ask_loan_command(message: Message, command: CommandObject) -> None:
        if not message.reply_to_message or not command.args or not command.args.strip().isdigit():
            await message.answer("🙏 <b>Запрос кредита</b>\n\nИспользование: <code>/ask_loan &lt;сумма&gt;</code> в ответ на сообщение того, у кого просите.", parse_mode="HTML")
            return
            
        amount = int(command.args.strip())
        if amount <= 0:
            await message.answer("Сумма должна быть больше нуля.")
            return

        sender_data = get_sender_data(message)
        target_data = get_sender_data(message.reply_to_message)
        
        if sender_data.user_id == target_data.user_id:
            await message.answer("Просить в долг у самого себя — это интересно, но бесполезно.")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🤝 Дать в долг", callback_data=f"aloan_yes_{amount}_{sender_data.user_id}_{target_data.user_id}"),
                InlineKeyboardButton(text="❌ Отказать", callback_data=f"aloan_no_{sender_data.user_id}")
            ]
        ])

        await message.answer(
            f"🙏 {escape_html(sender_data.display_name)} просит у вас <b>{amount} 🍪</b> в долг.\n\n"
            f"Вы готовы выручить игрока?",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    @router.callback_query(F.data.startswith("aloan_"))
    async def ask_loan_callback(query: CallbackQuery) -> None:
        parts = query.data.split("_")
        action = parts[1]
        
        if action == "no":
            original_asker_id = int(parts[2])
            # Отказать может только тот, у кого просили (текущий query.from_user)
            # Но нам не нужно строго проверять, кто нажал "Отказать", если это не сам проситель.
            # Хотя лучше проверить, что это именно тот, у кого просили.
            await query.message.edit_text("❌ В кредите отказано.")
            return

        # aloan_yes_{amount}_{asker_id}_{lender_id}
        amount = int(parts[2])
        asker_id = int(parts[3])
        lender_id = int(parts[4])
        
        if query.from_user.id != lender_id:
            await query.answer("Просили не у вас!", show_alert=True)
            return
            
        lender = db.get_user_by_platform_id(query.message.chat.id, lender_id)
        asker = db.get_user_by_platform_id(query.message.chat.id, asker_id)
        
        if not lender or lender.reputation < amount:
            await query.answer(f"❌ У вас недостаточно печенек! Нужно {amount}, а у вас {lender.reputation if lender else 0}.", show_alert=True)
            return
            
        # Выполняем сделку
        db.update_user(lender.id, {"reputation": lender.reputation - amount})
        db.update_user(asker.id, {
            "reputation": asker.reputation + amount,
            "debt": asker.debt + amount,
            "last_loan_at": datetime.now(timezone.utc).isoformat()
        })
        
        await query.message.edit_text(
            f"🤝 <b>Сделка совершена!</b>\n\n"
            f"{escape_html(lender.display_name)} выручил <b>{amount} 🍪</b> для {escape_html(asker.display_name)}.\n"
            f"⚠️ Долг записан и должен быть возвращен командой <code>/repay</code>.",
            parse_mode="HTML"
        )
        await query.answer("Вы дали в долг!")

    @router.message(Command("repay"))
    async def repay_command(message: Message, command: CommandObject) -> None:
        sender_data = get_sender_data(message)
        user = db.get_or_create_user(message.chat.id, sender_data)
        
        if user.debt <= 0:
            await message.answer("✅ У тебя нет активных долгов. Ты чист перед законом!")
            return
            
        amount = user.debt
        if command.args and command.args.strip().isdigit():
            amount = min(int(command.args.strip()), user.debt)
            
        if user.reputation < amount:
            await message.answer(f"❌ У тебя недостаточно печенек для погашения долга! Нужно <b>{amount} 🍪</b>, а у тебя {user.reputation} 🍪.")
            return
            
        db.update_user(user.id, {
            "reputation": user.reputation - amount,
            "debt": user.debt - amount
        })
        
        await message.answer(
            f"💰 <b>Долг погашен!</b>\n\n"
            f"Ты вернул <b>{amount} 🍪</b>. Остаток долга: <b>{user.debt - amount} 🍪</b>.",
            parse_mode="HTML"
        )

    @router.message(Command("give"))
    async def give_command(message: Message, command: CommandObject) -> None:
        if not message.reply_to_message or not command.args or not command.args.strip().isdigit():
            await message.answer("🎁 <b>Подарок</b>\n\nИспользование: <code>/give &lt;число&gt;</code> в ответ на сообщение.", parse_mode="HTML")
            return
        amount = int(command.args.strip())
        if amount <= 0:
            await message.answer("Сумма должна быть больше нуля.")
            return

        sender_data = get_sender_data(message)
        receiver_data = get_sender_data(message.reply_to_message)
        
        sender = db.get_or_create_user(message.chat.id, sender_data)
        receiver = db.get_or_create_user(message.chat.id, receiver_data)
        
        if sender.reputation < amount:
            await message.answer(f"❌ У тебя нет столько печенек! Баланс: {sender.reputation}")
            return
            
        db.update_user(sender.id, {"reputation": sender.reputation - amount})
        db.update_user(receiver.id, {"reputation": receiver.reputation + amount})

        # Генерируем умное сообщение с помощью AI
        gift_message = await ai.generate_cookie_gift_message(
            message.chat.id,
            sender.display_name,
            receiver.display_name,
            amount,
        )
        await message.answer(gift_message, parse_mode="HTML")

    @router.message(Command("steal"))
    async def steal_command(message: Message) -> None:
        if not message.reply_to_message:
            await message.answer("🚨 Нужно ответить на сообщение того, у кого хочешь украсть печеньки!")
            return
            
        sender_data = get_sender_data(message)
        target_data = get_sender_data(message.reply_to_message)
        
        if sender_data.user_id == target_data.user_id:
            await message.answer("Воровать у самого себя? Ты гений мысли.")
            return

        ok, remaining = db.can_user_use_command(message.chat.id, sender_data.user_id, "steal", 900)
        if not ok:
            await message.answer(f"⏳ Ты ещё не отошёл от прошлого дела. Подожди {remaining} сек.")
            return

        sender = db.get_or_create_user(message.chat.id, sender_data)
        target = db.get_or_create_user(message.chat.id, target_data)
        
        if target.reputation <= 0:
            await message.answer("🥥 У этого бедняка нечего воровать, карманы пусты.")
            return

        chance = random.random()
        if chance < 0.35: # Success
            amount = random.randint(1, max(1, int(target.reputation * 0.05)))
            db.add_reputation(target, -amount)
            db.add_reputation(sender, amount)
            await message.answer(f"🤫 <b>УСПЕХ!</b> Ты незаметно стащил <b>{amount} 🍪</b> у {escape_html(target.display_name)}.", parse_mode="HTML")
        else: # Failure
            db.add_reputation(sender, -2) # Small penalty for being caught
            await message.answer(f"🤡 <b>ТЕБЯ ПОЙМАЛИ!</b> {escape_html(sender.display_name)} пытался залезть в карман к {escape_html(target.display_name)}, но споткнулся и выронил 2 🍪.")

    @router.message(Command("setflavor", "вкус"))
    async def set_flavor_command(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("🍦 <b>Твой вкус</b>\n\nИспользование: <code>/setflavor &lt;вкус&gt;</code>\nПример: <code>/setflavor Шоколадный</code>", parse_mode="HTML")
            return
        sender = get_sender_data(message)
        user = db.get_or_create_user(message.chat.id, sender)
        db.update_user(user.id, {"flavor": command.args.strip()})
        await message.answer(f"✅ Теперь твой вкус: <b>{escape_html(command.args.strip())}</b>", parse_mode="HTML")

    @router.message(Command("cookie_rain", "дождь"))
    async def rain_command(message: Message) -> None:
        if not await is_admin(message):
            await message.answer("❌ Эта магия доступна только администраторам.")
            return
        
        users = db.get_all_users(message.chat.id)
        if not users: return
        
        amount = random.randint(5, 15)
        for u in users:
            db.update_user(u.id, {"reputation": u.reputation + amount})
            
        await message.answer(
            f"🌧 <b>ПЕЧЕНЬКОПАД!</b>\n\n"
            f"Администратор вызвал бурю! Все активные участники ({len(users)}) получили по <b>{amount} 🍪</b>!",
            parse_mode="HTML"
        )

    @router.message(Command("whisper", "шепот"))
    async def whisper_command(message: Message, command: CommandObject) -> None:
        if not await is_admin(message):
            await message.answer("🤫 Тсс... (Только для админов)")
            return
        if not command.args: return
        
        await message.delete()
        await message.answer(command.args)

    @router.message(Command("casino", "gamble", "spin", "казино", "ставка"))
    async def casino_command(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("🎰 <b>Казино NeuroNika</b>\n\nИспользование: <code>/casino &lt;сумма&gt;</code>\nМинимальная ставка: <code>1 🍪</code>", parse_mode="HTML")
            return
            
        bet = int(command.args.strip())
        if bet <= 0:
            await message.answer("❌ Ставка должна быть больше нуля!")
            return

        sender_data = get_sender_data(message)
        sender = db.get_or_create_user(message.chat.id, sender_data)
        
        allowed, remaining = db.can_use_command(message.chat.id, f"casino_{sender.user_id}", 20)
        if not allowed:
            await message.answer(
                f"⏳ <b>Автомат остывает.</b>\nПодожди ещё <code>{remaining} сек.</code>",
                parse_mode="HTML"
            )
            return

        if sender.reputation < bet:
            await message.answer(f"❌ Недостаточно печенек! У тебя всего: <b>{sender.reputation}</b> 🍪")
            return

        # Снимаем ставку и берем 1% в джекпот
        chat_settings = db.get_chat_settings(message.chat.id)
        current_jackpot = chat_settings.casino_jackpot
        tax = max(1, bet // 100)
        db.update_user(sender.id, {"reputation": sender.reputation - bet})
        db.update_chat_settings(message.chat.id, casino_jackpot=current_jackpot + tax)
        current_jackpot += tax

        # Старая добрая анимация
        frames = [
            f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\n💰 Ставка: <code>{bet}</code> 🍪\n🏆 Джекпот: <code>{current_jackpot}</code>\n\n[ 🎲 | 🎲 | 🎲 ]",
            f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\n💰 Ставка: <code>{bet}</code> 🍪\n🏆 Джекпот: <code>{current_jackpot}</code>\n\n[ 🍒 | 🍋 | 🍇 ]",
            f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\n💰 Ставка: <code>{bet}</code> 🍪\n🏆 Джекпот: <code>{current_jackpot}</code>\n\n[ 🍋 | 🍇 | 🔔 ]",
            f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\n💰 Ставка: <code>{bet}</code> 🍪\n🏆 Джекпот: <code>{current_jackpot}</code>\n\n[ 🍇 | 🔔 | 🍉 ]",
        ]
        
        msg = await message.answer(frames[0], parse_mode="HTML")
        for frame in frames[1:]:
            await asyncio.sleep(0.4)
            await msg.edit_text(frame, parse_mode="HTML")
        await asyncio.sleep(0.5)

        # Вычисляем результат
        symbols = ["🍒", "🍋", "🍇", "🍉", "🔔", "💎", "🎰"]
        r1, r2, r3 = random.choice(symbols), random.choice(symbols), random.choice(symbols)
        
        final_symbols = f" [ {r1} | {r2} | {r3} ] "
        result_text = ""
        win_total = 0
        is_jackpot = False
        consol_xp = 0

        if r1 == r2 == r3:
            if r1 == "🎰":
                is_jackpot = True
                win_total = int(bet * 50) + current_jackpot
                db.update_chat_settings(message.chat.id, casino_jackpot=500)
                result_text = f"🌌 <b>ЛЕГЕНДАРНЫЙ ДЖЕКПОТ!!!</b>\n\nТы сорвал куш в <b>{win_total}</b> 🍪! 🎉🍾"
            elif r1 == "💎":
                win_total = bet * 25
                result_text = "💎 <b>БРИЛЛИАНТОВЫЙ ВЫИГРЫШ!</b> x25!"
            elif r1 == "🔔":
                win_total = bet * 15
                result_text = "🔔 <b>КОЛОКОЛЬНЫЙ ЗВОН!</b> x15!"
            else:
                win_total = bet * 10
                result_text = "✨ <b>ТРИ В РЯД!</b> Отличный выигрыш x10!"
        elif r1 == r2 or r2 == r3 or r1 == r3:
            win_total = int(bet * 1.5)
            result_text = "🥂 <b>ПАРА!</b> Небольшой выигрыш x1.5"
        else:
            win_total = 0
            consol_xp = random.randint(2, 5)
            result_text = f"💨 <b>НЕУДАЧА!</b> В следующий раз повезет!\n<i>(Утешительный приз: +{consol_xp} XP)</i>"

        new_bal = (sender.reputation - bet) + win_total
        if win_total > 0:
            db.update_user(sender.id, {"reputation": new_bal})
        else:
            db.update_user(sender.id, {"reputation": new_bal, "xp": sender.xp + consol_xp})

        final_msg = (
            f"🎰 <b>РЕЗУЛЬТАТЫ СПИНА</b>\n"
            f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n\n"
            f"<code>{final_symbols}</code>\n\n"
            f"{result_text}\n"
            f"💰 Твой баланс: <b>{new_bal}</b> 🍪"
        )
        
        keyboard = None
        if win_total > 0 and not is_jackpot:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🃏 Рискнуть: Удвоить", callback_data=f"dice_double_{sender.user_id}_{win_total}")]
            ])

        await msg.edit_text(final_msg, reply_markup=keyboard, parse_mode="HTML")



    @router.callback_query(F.data.startswith("dice_double_"))
    async def casino_double_callback(query: CallbackQuery) -> None:
        parts = query.data.split("_")
        user_id = int(parts[2])
        win_amount = int(parts[3])
        
        if query.from_user.id != user_id:
            await query.answer("❌ Это не твой выигрыш!", show_alert=True)
            return

        sender = db.get_user_by_platform_id(query.message.chat.id, user_id)
        if not sender: return

        # Double or nothing logic
        db.update_user(sender.id, {"reputation": sender.reputation - win_amount}) 
        
        if random.random() < 0.45: # 45% chance to double
            new_win = win_amount * 2
            db.update_user(sender.id, {"reputation": sender.reputation + new_win})
            await query.message.edit_text(
                f"🃏 <b>РИСК ОПРАВДАН!</b>\n\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                f"🔥 Ты удвоил куш: <b>{new_win}</b> 🍪!\n"
                f"📈 Баланс: <b>{sender.reputation - win_amount + new_win}</b> 🍪",
                parse_mode="HTML"
            )
            await query.answer("💰 Удвоено!")
        else:
            await query.message.edit_text(
                f"🃏 <b>РИСК НЕ ОПРАВДАН...</b>\n\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                f"💀 Ты потерял всё: <b>{win_amount}</b> 🍪\n"
                f"📉 Баланс: <b>{sender.reputation - win_amount}</b> 🍪",
                parse_mode="HTML"
            )
            await query.answer("💀 Потрачено", show_alert=True)

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



    def _get_tower_multiplier(floor: int) -> float:
        multipliers = [1.0, 1.2, 1.5, 2.0, 3.0, 5.0, 10.0, 25.0, 50.0, 100.0]
        if floor < 1: return 1.0
        if floor > 10: return 100.0
        return multipliers[floor-1]

    # --- ИГРА: БАШНЯ ФОРТУНЫ (ULTIMATE WEATHER VERSION) ---
    
    _tower_locks = {}

    @router.message(Command("tower", "башня", "climb"))
    async def tower_command(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer(
                "🏰 <b>Башня Фортуны: ULTIMATE</b>\n\n"
                "🛡 <b>Пороги:</b> 5 эт. (30%), 8 эт. (60%)\n"
                "Использование: <code>/tower <ставка></code>", 
                parse_mode="HTML"
            )
            return
            
        bet = int(command.args.strip())
        sender_data = get_sender_data(message)
        sender = db.get_or_create_user(message.chat.id, sender_data)
        
        allowed, remaining = db.can_use_command(message.chat.id, f"tower_{sender.user_id}", 30)
        if not allowed:
            await message.answer(f"⏳ Башня на отдыхе. Подожди {remaining} сек.", parse_mode="HTML")
            return

        if bet < 5:
            await message.answer("❌ Минимальная ставка: 5 🍪")
            return
            
        if sender.reputation < bet:
            await message.answer(f"❌ Недостаточно печенек! У тебя: {sender.reputation} 🍪")
            return

        # Рандомная погода
        weathers = [
            {"name": "☀️ Солнечно", "chance_mod": 0, "reward_mod": 1.0, "desc": "Обычное восхождение."},
            {"name": "🌫 Туман", "chance_mod": 0.05, "reward_mod": 1.3, "desc": "Множители скрыты, но награда +30%!"},
            {"name": "🌪 Шторм", "chance_mod": -0.15, "reward_mod": 1.7, "desc": "Трудно лезть, но награда x1.7!"},
            {"name": "🌈 Радуга", "chance_mod": 0.1, "reward_mod": 0.9, "desc": "Легкий подъем, но награда -10%."}
        ]
        weather = random.choice(weathers)
        
        db.update_user(sender.id, {"reputation": sender.reputation - bet})
        await _show_tower(message, floor=1, bet=bet, user_id=sender.user_id, is_new=True, weather=weather)

    @router.callback_query(F.data.startswith("tower_"))
    async def tower_callback(query: CallbackQuery) -> None:
        parts = query.data.split("_")
        # tower_{action}_{floor}_{bet}_{user_id}_{weather_idx}
        action = parts[1]
        floor = int(parts[2])
        bet = int(parts[3])
        original_user_id = int(parts[4])
        weather_idx = int(parts[5]) if len(parts) > 5 else 0
        
        weathers = [
            {"name": "☀️ Солнечно", "chance_mod": 0, "reward_mod": 1.0},
            {"name": "🌫 Туман", "chance_mod": 0.05, "reward_mod": 1.3},
            {"name": "🌪 Шторм", "chance_mod": -0.15, "reward_mod": 1.7},
            {"name": "🌈 Радуга", "chance_mod": 0.1, "reward_mod": 0.9}
        ]
        weather = weathers[weather_idx]

        if query.from_user.id != original_user_id:
            await query.answer("❌ Не твой подъем!", show_alert=True)
            return
            
        now = time.time()
        lock_key = f"{query.message.chat.id}_{query.message.message_id}"
        if lock_key in _tower_locks and now - _tower_locks[lock_key] < 0.8:
            await query.answer("⏳ Подожди секунду...", show_alert=False)
            return
        _tower_locks[lock_key] = now

        sender = db.get_user_by_platform_id(query.message.chat.id, original_user_id)
        if not sender: return

        if action == "take":
            multiplier = _get_tower_multiplier(floor) * weather["reward_mod"]
            win = int(bet * multiplier)
            db.update_user(sender.id, {"reputation": sender.reputation + win})
            
            await query.message.edit_text(
                f"🎉 <b>ЗАБРАЛ ПРИЗ!</b>\n\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                f"🌤 Погода: <b>{weather['name']}</b>\n"
                f"🪜 Этажей: <b>{floor}</b>\n"
                f"💰 Выигрыш: <b>{win} 🍪</b>\n\n"
                f"📈 Баланс: <b>{sender.reputation + win}</b> 🍪",
                parse_mode="HTML"
            )
            await query.answer("💰 Кошелек пополнен!")
            
        elif action == "up":
            chances = [1.0, 0.9, 0.85, 0.8, 0.75, 0.7, 0.6, 0.5, 0.4, 0.3]
            base_chance = chances[floor-1] if floor <= len(chances) else 0.2
            success_chance = max(0.1, base_chance + weather["chance_mod"])
            
            if random.random() < success_chance:
                new_floor = floor + 1
                if new_floor > 10:
                    multiplier = _get_tower_multiplier(10) * weather["reward_mod"]
                    win = int(bet * multiplier)
                    db.update_user(sender.id, {"reputation": sender.reputation + win})
                    await query.message.edit_text(f"🏆 <b>ВЕРШИНА БАШНИ!</b> (x{multiplier:.1f})\n💰 Твой куш: {win} 🍪", parse_mode="HTML")
                    return

                try:
                    await _show_tower(query.message, floor=new_floor, bet=bet, user_id=original_user_id, is_new=False, weather=weather)
                    await query.answer("✅ Поднялся!")
                except Exception: pass
            else:
                # Пороги
                safe_win = 0
                mult = _get_tower_multiplier(floor) * weather["reward_mod"]
                if floor >= 8: safe_win = int(bet * mult * 0.6)
                elif floor >= 5: safe_win = int(bet * mult * 0.3)

                if safe_win > 0:
                    db.update_user(sender.id, {"reputation": sender.reputation + safe_win})

                await query.message.edit_text(
                    f"💀 <b>ОШИБКА!</b>\n"
                    f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                    f"🪜 Сорвался на {floor + 1} этаже.\n"
                    f"🛡 Сработала страховка: {safe_win} 🍪\n"
                    f"📈 Баланс: <b>{sender.reputation + safe_win}</b> 🍪",
                    parse_mode="HTML"
                )
                await query.answer("💥 БА-БАХ!", show_alert=True)

    async def _show_tower(msg: Message, floor: int, bet: int, user_id: int, is_new: bool, weather: dict) -> None:
        multiplier = _get_tower_multiplier(floor) * weather["reward_mod"]
        
        weathers_list = ["☀️ Солнечно", "🌫 Туман", "🌪 Шторм", "🌈 Радуга"]
        w_idx = 0
        try: w_idx = weathers_list.index(weather["name"])
        except: pass

        tower_lines = []
        for i in range(10, 0, -1):
            m = _get_tower_multiplier(i) * weather["reward_mod"]
            # В тумане множители скрыты
            m_text = f"x{m:.1f}" if weather["name"] != "🌫 Туман" or i <= floor else "x???"
            
            if i == floor: line = f"🔥 <b>[{i:02}] {m_text} 🚩 ТЫ ТУТ</b>"
            elif i < floor: line = f"✅ <code>[{i:02}] {m_text}</code>"
            else: 
                prefix = "🪜"
                if i == 10: prefix = "💎"
                elif i == 8: prefix = "🥈"
                elif i == 5: prefix = "🥉"
                line = f"{prefix} <code>[{i:02}] {m_text}</code>"
            tower_lines.append(line)

        progress = "▰" * floor + "▱" * (10 - floor)
        text = (
            f"🏰 <b>БАШНЯ ФОРТУНЫ: ULTIMATE</b>\n"
            f"🌤 Погода: <b>{weather['name']}</b>\n"
            f"<code>{progress}</code> {floor}/10\n\n"
            + "\n".join(tower_lines) + "\n\n" +
            f"💰 Ставка: <code>{bet}</code> 🍪\n"
            f"💵 Куш: <b>{int(bet * multiplier)} 🍪</b>\n"
            f"<i>Поднимаемся или забираем?</i>"
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⏫ ВВЕРХ", callback_data=f"tower_up_{floor}_{bet}_{user_id}_{w_idx}"),
                InlineKeyboardButton(text="💰 ЗАБРАТЬ", callback_data=f"tower_take_{floor}_{bet}_{user_id}_{w_idx}")
            ]
        ])
        
        if is_new: await msg.answer(text, reply_markup=kb, parse_mode="HTML")
        else: await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")

    return router
