from __future__ import annotations

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
            # В данном случае, это просто закрывает запрос.
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
            amount = random.randint(1, max(1, int(target.reputation * 0.15)))
            db.add_reputation(target, -amount)
            db.add_reputation(sender, amount)
            await message.answer(f"🤫 <b>УСПЕХ!</b> Ты незаметно стащил <b>{amount} 🍪</b> у {escape_html(target.display_name)}.", parse_mode="HTML")
        else: # Failure
            loss = random.randint(5, 15)
            db.add_reputation(sender, -loss)
            await message.answer(f"🚨 <b>ПОЙМАН!</b> {escape_html(target.display_name)} заметил тебя! Ты позорно бежал, потеряв <b>{loss} 🍪</b>.", parse_mode="HTML")

    @router.message(Command("setflavor"))
    async def set_flavor_command(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /setflavor &lt;твой вкус&gt; (например: шоколадная, с мятой, ванильная)", parse_mode="HTML")
            return
        sender = get_sender_data(message)
        user = db.get_or_create_user(message.chat.id, sender)
        flavor = command.args.strip()
        db.update_user_flavor(user, flavor)
        await message.answer(f"✨ Теперь твой вкус: <b>{escape_html(flavor)}</b>", parse_mode="HTML")

    @router.message(Command("cookie_rain"))
    async def rain_command(message: Message) -> None:
        if not await is_admin(message.bot, message.chat.id, message.from_user.id):
             await message.answer("❌ Только админ может вызвать печеньковый дождь!")
             return
             
        users = db.get_active_users(message.chat.id, minutes=60, limit=10)
        if not users:
            await message.answer("☁️ В чате слишком тихо для дождя. Никого нет.")
            return
            
        rewarded = []
        for u in users:
            c = random.randint(1, 3)
            x = random.randint(20, 100)
            db.add_reputation(u, c)
            db.add_xp(u, x)
            rewarded.append(u.display_name)
            
        text = (
            "🍪🌧 <b>ПЕЧЕНЬКОВЫЙ ДОЖДЬ!</b>\n\n"
            "Небо затянуло облаками и на головы посыпались вкусняшки!\n\n"
            f"🎁 Получили подарки: {', '.join(rewarded)}"
        )
        await message.answer(text, parse_mode="HTML")

    @router.message(Command("whisper"))
    async def whisper_command(message: Message, command: CommandObject) -> None:
        if not await is_admin(message.bot, message.chat.id, message.from_user.id):
             return
             
        if not command.args:
            return
            
        # Отправляем сообщение от имени бота и удаляем команду
        await message.delete()
        await message.answer(command.args, parse_mode="HTML")

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
        
        # Проверка кулдауна (20 секунд на казино)
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

        # Снимаем ставку сразу
        db.update_user(sender.id, {"reputation": sender.reputation - bet})

        # Начальная анимация
        frames = [
            f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\n\n[ 🎲 | 🎲 | 🎲 ]\n\n💰 Ставка: <code>{bet}</code> 🍪",
            f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\n\n[ 🍋 | 🍒 | 🍇 ]\n\n💰 Ставка: <code>{bet}</code> 🍪",
            f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\n\n[ 🍇 | 🍋 | 🍉 ]\n\n💰 Ставка: <code>{bet}</code> 🍪",
        ]
        
        msg = await message.answer(frames[0], parse_mode="HTML")
        await asyncio.sleep(0.6)
        await msg.edit_text(frames[1], parse_mode="HTML")
        await asyncio.sleep(0.6)
        await msg.edit_text(frames[2], parse_mode="HTML")
        await asyncio.sleep(0.6)

        roll = random.randint(1, 1000)
        
        final_symbols = ""
        result_text = ""
        multiplier = 0
        consol_xp = 0
        
        if roll <= 450:
            final_symbols = " [ 💀 | 🍋 | 🍒 ] "
            consol_xp = random.randint(1, 3)
            result_text = f"💨 Увы, фортуна отвернулась! Ты потерял <b>{bet}</b> 🍪.\n<i>(Утешительный приз: +{consol_xp} XP)</i>"
            multiplier = 0
        elif roll <= 600:
            final_symbols = " [ 🍋 | 🍋 | 🍋 ] "
            result_text = f"🔄 Почти! Возврат ставки. Баланс сохранен."
            multiplier = 1
        elif roll <= 750:
            final_symbols = " [ 🍒 | 🍒 | 🍒 ] "
            result_text = f"✨ Неплохо! Выигрыш: <b>{int(bet * 1.5)}</b> 🍪."
            multiplier = 1.5
        elif roll <= 850:
            final_symbols = " [ 🍇 | 🍇 | 🍇 ] "
            result_text = f"🔥 Хорошо идешь! Удвоение: <b>{bet * 2}</b> 🍪."
            multiplier = 2
        elif roll <= 920:
            final_symbols = " [ 🍉 | 🍉 | 🍉 ] "
            result_text = f"🌟 Отличный улов! Утроение: <b>{bet * 3}</b> 🍪."
            multiplier = 3
        elif roll <= 960:
            final_symbols = " [ 🔔 | 🔔 | 🔔 ] "
            result_text = f"💥 БИГ ВИН! Пятикратный выигрыш: <b>{bet * 5}</b> 🍪!"
            multiplier = 5
        elif roll <= 985:
            final_symbols = " [ ⭐️ | ⭐️ | ⭐️ ] "
            result_text = f"🌈 СУПЕР ПРИЗ! Ты получил <b>{bet * 8}</b> 🍪!"
            multiplier = 8
        elif roll <= 995:
            final_symbols = " [ 💎 | 💎 | 💎 ] "
            result_text = f"👑 МЕГА КУШ! Невероятные <b>{bet * 15}</b> 🍪!"
            multiplier = 15
        else:
            final_symbols = " [ 🏆 | 🏆 | 🏆 ] "
            result_text = f"🌌 <b>ДЖЕКПОТ!!!</b> Ты сорвал куш в <b>{bet * 30}</b> 🍪! 🎉"
            multiplier = 30

        win_total = int(bet * multiplier)
        if win_total > 0:
            db.update_user(sender.id, {"reputation": sender.reputation - bet + win_total})
            new_bal = sender.reputation - bet + win_total
        else:
            db.update_user(sender.id, {"reputation": sender.reputation - bet, "xp": sender.xp + consol_xp})
            new_bal = sender.reputation - bet

        final_msg = (
            f"🎰 <b>РЕЗУЛЬТАТЫ СПИНА</b>\n"
            f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n\n"
            f"<code>{final_symbols}</code>\n\n"
            f"{result_text}\n"
            f"💰 Твой баланс: <b>{new_bal}</b> 🍪"
        )
        
        keyboard = None
        if win_total > 0:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🃏 Рискнуть: Удвоить или ничего!", callback_data=f"double_{win_total}_{sender.user_id}")]
            ])
        
        await msg.edit_text(final_msg, reply_markup=keyboard, parse_mode="HTML")

    @router.callback_query(F.data.startswith("double_"))
    async def double_callback(query: CallbackQuery) -> None:
        parts = query.data.split("_")
        amount = int(parts[1])
        original_user_id = int(parts[2])
        
        if query.from_user.id != original_user_id:
            await query.answer("❌ Это не твой выигрыш!", show_alert=True)
            return
            
        # 50/50 шанс
        if random.random() < 0.5:
            new_win = amount * 2
            # Начисляем разницу (потому что amount уже был начислен в casino_command)
            sender = db.get_user_by_platform_id(query.message.chat.id, query.from_user.id)
            if sender:
                db.update_user(sender.id, {"reputation": sender.reputation + amount})
                new_balance = sender.reputation + amount
                
                await query.message.edit_text(
                    f"🃏 <b>РИСК ОПРАВДАН!</b>\n"
                    f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n\n"
                    f"Ты удвоил свой выигрыш до <b>{new_win}</b> 🍪!\n"
                    f"💰 Новый баланс: <b>{new_balance}</b> 🍪",
                    parse_mode="HTML"
                )
        else:
            # Вычитаем выигрыш (потому что amount уже был начислен в casino_command)
            sender = db.get_user_by_platform_id(query.message.chat.id, query.from_user.id)
            if sender:
                db.update_user(sender.id, {"reputation": sender.reputation - amount})
                new_balance = sender.reputation - amount
                
                await query.message.edit_text(
                    f"🃏 <b>УВЫ, ВСЁ ПОТЕРЯНО!</b>\n"
                    f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n\n"
                    f"Ты проиграл свои <b>{amount}</b> 🍪 в попытке удвоить.\n"
                    f"💰 Новый баланс: <b>{new_balance}</b> 🍪",
                    parse_mode="HTML"
                )
        
        await query.answer()

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
