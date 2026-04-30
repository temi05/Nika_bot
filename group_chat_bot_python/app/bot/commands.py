from __future__ import annotations
import time

import asyncio
import base64
import random
from datetime import datetime, timezone, timedelta
from io import BytesIO

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.services.ai_service import AIService
from app.services.supabase_db import SupabaseDB
from app.bot.admin import is_admin
from app.models import MemoryRecord, Sender
from app.utils import (
    build_progress_bar,
    escape_html,
    get_sender_data,
    human_timedelta,
    parse_birthday_parts,
)

AUTO_DROP_SESSIONS: dict[str, dict] = {}
AUTO_QUIZ_SESSIONS: dict[str, dict] = {}
AUTO_CLAIMED_EVENTS: set[str] = set()
DUEL_SESSIONS: dict[str, dict] = {}
NIKA_REFERENCE_ASSET_KEY = "nika_reference"


def make_quiz_question() -> dict:
    a = random.randint(3, 18)
    b = random.randint(2, 15)
    op = random.choice(["+", "-", "*"])
    if op == "+":
        answer = a + b
        question = f"{a} + {b}"
    elif op == "-":
        a, b = max(a, b), min(a, b)
        answer = a - b
        question = f"{a} - {b}"
    else:
        answer = a * b
        question = f"{a} × {b}"

    options = {answer}
    while len(options) < 4:
        options.add(max(0, answer + random.randint(-12, 12)))
    option_list = list(options)
    random.shuffle(option_list)
    return {
        "question": question,
        "answer_idx": option_list.index(answer),
        "options": option_list,
        "reward": random.randint(14, 32),
    }


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
            "• <code>/debts</code> — Список твоих долгов и должников\n"
            "• <code>/forgive [сумма]</code> — Простить долг (реплаем)\n"
            "• <code>/steal</code> — Попробовать украсть (реплаем)\n"
            "• <code>/jail</code>, <code>/bail</code> — Тюрьма и залог\n"
            "• <code>/top</code> — Рейтинг самых богатых"
        ),
        "games": (
            "🎰 <b>Игры и Развлечения</b>\n\n"
            "• <code>/casino [ставка]</code> — Премиальный слот-автомат\n"
            "• <code>/tower [ставка]</code> — Рискованная Башня (до x40)\n"
            "• <code>/coin [ставка] [орел/решка]</code> — Монетка x1.9\n"
            "• <code>/rps [ставка] [камень/ножницы/бумага]</code> — Дуэль с ботом\n"
            "• <code>/duel [ставка] [камень/ножницы/бумага]</code> — Дуэль с игроком (реплаем)\n"
            "• <code>/fish</code> — Рыбалка за печеньками\n"
            "• <code>/aiimage [описание]</code> — ИИ-картинка за печеньки\n"
            "• <code>/signai [текст]</code> — ИИ-сигна за печеньки\n"
            "• <code>/setsignprice</code>, <code>/signreq</code>, <code>/signorders</code> — Сигны от людей\n"
            "• Авто-событие: печенька падает в активный чат\n"
            "• Авто-событие: викторина, первый правильный ответ получает приз\n"
            "• <code>/rp [действие]</code> — Ролевые взаимодействия"
        ),
        "profile": (
            "👤 <b>Персонализация</b>\n\n"
            "• <code>/bio [текст]</code> — Расскажи о себе\n"
            "• <code>/mybirthday [дата]</code> — Установи день рождения\n"
            "• <code>/remember [факт]</code> — Явно сохранить важный факт"
        ),
        "admin": (
            "🛡 <b>Администрирование</b>\n\n"
            "• <code>/cookie_rain</code> — Массовая раздача бонусов\n"
            "• <code>/whisper [текст]</code> — Сообщение от имени бота\n"
            "• <code>/judge</code> — Посадить или выпустить игрока\n"
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
            lines.append(f"🍪 Печеньки: <b>+{result.get('bonus_reputation', 0)}</b>")
        if result.get("lucky_bonus", 0) > 0:
            lines.append("💫 Редкий ежедневный бонус: +100 🍪")
        if result["level_up"]:
            lines.append(f"🎉 Новый уровень: <b>{result['new_level']}</b>")
        lines.append(f"💼 Баланс: <code>{result['new_reputation']}</code> 🍪")
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

    @router.message(Command("remember", "запомни"))
    async def remember_command(message: Message, command: CommandObject) -> None:
        fact = (command.args or "").strip()
        if not fact:
            await message.answer("Использование: <code>/remember факт, который надо запомнить</code>", parse_mode="HTML")
            return
        sender = get_sender_data(message)
        db.get_or_create_user(message.chat.id, sender)
        db.store_memory(
            message.chat.id,
            MemoryRecord(
                fact=f"{sender.display_name}: {fact[:500]}",
                source="manual_memory",
                confidence=0.98,
                meta={"user_id": sender.user_id, "user_name": sender.display_name},
            ),
        )
        await message.answer("🧠 Запомнила. Лишнего вокруг этого сообщения в память не беру.")

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
            "1. Купить 1 уровень — <code>1200 🍪</code>\n"
            "   Команда: <code>/buy 1</code>\n\n"
            "2. Снять все предупреждения — <code>600 🍪</code>\n"
            "   Команда: <code>/buy 2</code>\n\n"
            f"3. ИИ-сигна — <code>{ai.settings.ai_sign_price} 🍪</code>\n"
            "   Команда: <code>/signai текст на сигне</code>\n"
            "   На сигне будет Ника в случайной позе/локации\n\n"
            f"4. ИИ-картинка — <code>{ai.settings.ai_image_price} 🍪</code>\n"
            "   Команда: <code>/aiimage описание картинки</code>\n\n"
            f"5. Сигна от человека — цена автора, минимум <code>{ai.settings.human_sign_min_price} 🍪</code>\n"
            "   Базовая цена: <code>/setsignprice сумма</code>\n"
            "   Тариф: <code>/setsignprice название цена описание</code>\n"
            "   Цены: ответь человеку <code>/signprice</code>\n"
            "   Заказ: ответь человеку <code>/signreq текст</code> или <code>/signreq # текст</code>\n"
            "   На human-сигне должен быть автор, у которого заказали\n"
            "   Мои заказы: <code>/signorders</code>, <code>/signorders мои</code>\n\n"
            "<i>Основной стабильный доход — /daily, активность и редкие чат-события. Мини-игры нужны для риска, а не бесконечного фарма.</i>",
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

    def _parse_iso_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _jail_remaining(user) -> timedelta | None:
        jailed_until = _parse_iso_dt(user.jailed_until)
        if not jailed_until:
            return None
        remaining = jailed_until - datetime.now(timezone.utc)
        if remaining.total_seconds() <= 0:
            db.update_user(
                user.id,
                {
                    "jailed_until": None,
                    "jail_reason": None,
                    "steal_fail_streak": 0,
                    "steal_success_streak": 0,
                },
            )
            return None
        return remaining

    def _format_remaining(delta: timedelta) -> str:
        seconds = max(0, int(delta.total_seconds()))
        minutes = seconds // 60
        if minutes < 60:
            return f"{max(1, minutes)} мин."
        return f"{minutes // 60} ч. {minutes % 60} мин."

    async def _deny_if_jailed(message: Message, user, action_name: str) -> bool:
        _maybe_jail_for_overdue_debt(user)
        user = db.get_user_by_platform_id(user.chat_id, user.user_id) or user
        remaining = _jail_remaining(user)
        if not remaining:
            return False
        await message.answer(
            f"🚔 <b>Ты сейчас в тюрьме.</b>\n"
            f"Действие <code>{escape_html(action_name)}</code> недоступно ещё <b>{_format_remaining(remaining)}</b>.\n"
            f"Причина: <i>{escape_html(user.jail_reason or 'нарушение')}</i>\n\n"
            f"Можно выйти через <code>/bail</code>.",
            parse_mode="HTML",
        )
        return True

    def _loan_limit(user) -> int:
        return max(50, user.level * 25 + user.reputation)

    def _bail_cost(user) -> int:
        base_cost = max(50, user.level * 20, int(user.debt * 0.35))
        wealth_part = int(user.reputation * 0.20)
        return base_cost + wealth_part

    def _pay_bail(user, cost: int) -> dict[str, int]:
        requested_creditor_part = min(user.debt, max(0, cost // 2))
        creditor_part = 0
        if requested_creditor_part > 0:
            result = db.repay_debts(user, requested_creditor_part)
            creditor_part = int(result.get("paid") or 0)
            user = db.get_user_by_platform_id(user.chat_id, user.user_id) or user
        fee = cost - creditor_part
        db.update_user(
            user.id,
            {
                "reputation": max(0, user.reputation - fee),
                "jailed_until": None,
                "jail_reason": None,
                "steal_fail_streak": 0,
            },
        )
        return {"creditor_part": creditor_part, "fee": fee}

    def _jail_user(user, minutes: int, reason: str) -> None:
        db.update_user(
            user.id,
            {
                "jailed_until": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
                "jail_reason": reason,
            },
        )

    def _maybe_jail_for_overdue_debt(user) -> None:
        if user.debt <= 0 or _jail_remaining(user):
            return
        active_debts = db.get_active_debts_for_borrower(user.chat_id, user.user_id)
        now = datetime.now(timezone.utc)
        has_overdue = any((_parse_iso_dt(debt.get("due_at")) or now) < now for debt in active_debts)
        if has_overdue:
            _jail_user(user, 180, "просроченный долг")

    _loan_sessions = {}
    _bail_sessions = {}

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
        target = db.get_or_create_user(message.chat.id, target_data)
        if target.debt + amount > _loan_limit(target):
            await message.answer(
                f"❌ У {escape_html(target.display_name)} слишком большой долг.\n"
                f"Лимит: <b>{_loan_limit(target)} 🍪</b>, сейчас: <b>{target.debt} 🍪</b>.",
                parse_mode="HTML",
            )
            return

        if sender.reputation < amount:
            await message.answer(f"❌ Недостаточно печенек! Баланс: {sender.reputation}")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"loan_acc_{amount}_{sender.user_id}_{target_data.user_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"loan_dec_{target_data.user_id}")
            ]
        ])

        sent_msg = await message.answer(
            f"🤝 {escape_html(sender.display_name)} предлагает вам <b>{amount} 🍪</b> в долг.\n\n"
            f"Вы согласны принять кредит?",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        _loan_sessions[f"{message.chat.id}_{sent_msg.message_id}"] = {
            "kind": "offer",
            "amount": amount,
            "lender_id": sender.user_id,
            "borrower_id": target_data.user_id,
        }

    @router.callback_query(F.data.startswith("loan_"))
    async def loan_callback(query: CallbackQuery) -> None:
        parts = query.data.split("_")
        action = parts[1]
        session_key = f"{query.message.chat.id}_{query.message.message_id}"
        session = _loan_sessions.get(session_key)
        if not session:
            await query.answer("⏳ Это предложение уже неактивно.", show_alert=True)
            return
        
        if action == "dec":
            target_id = int(parts[2])
            if query.from_user.id != target_id:
                await query.answer("Это не вам предложили!", show_alert=True)
                return
            _loan_sessions.pop(session_key, None)
            await query.message.edit_text("❌ Предложение отклонено.")
            return

        # loan_acc_{amount}_{sender_id}_{target_id}
        amount = int(parts[2])
        sender_id = int(parts[3])
        target_id = int(parts[4])
        if (
            session.get("kind") != "offer"
            or session.get("amount") != amount
            or session.get("lender_id") != sender_id
            or session.get("borrower_id") != target_id
        ):
            await query.answer("❌ Кнопка не совпадает с предложением.", show_alert=True)
            return
        
        if query.from_user.id != target_id:
            await query.answer("Это не вам предложили!", show_alert=True)
            return
            
        sender = db.get_user_by_platform_id(query.message.chat.id, sender_id)
        target = db.get_user_by_platform_id(query.message.chat.id, target_id)
        
        if not sender or sender.reputation < amount:
            await query.message.edit_text("❌ Ошибка: у отправителя больше нет нужной суммы.")
            return
        if not target:
            await query.message.edit_text("❌ Ошибка: получатель не найден.")
            return
        if target.debt + amount > _loan_limit(target):
            await query.message.edit_text("❌ Сделка отменена: у получателя уже слишком большой долг.")
            return
             
        # Выполняем сделку
        _loan_sessions.pop(session_key, None)
        db.update_user(sender.id, {"reputation": sender.reputation - amount})
        db.update_user(target.id, {
            "reputation": target.reputation + amount,
            "debt": target.debt + amount,
            "last_loan_at": datetime.now(timezone.utc).isoformat()
        })
        db.create_debt(sender, target, amount)
        
        await query.message.edit_text(
            f"🤝 <b>Сделка совершена!</b>\n\n"
            f"{escape_html(sender.display_name)} одолжил <b>{amount} 🍪</b> {escape_html(target.display_name)}.\n"
            f"⚠️ Долг должен быть возвращен командой <code>/repay</code> в течение 48 часов.",
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

        asker = db.get_or_create_user(message.chat.id, sender_data)
        if asker.debt + amount > _loan_limit(asker):
            await message.answer(
                f"❌ Сначала погаси часть старых долгов.\n"
                f"Твой лимит: <b>{_loan_limit(asker)} 🍪</b>, сейчас: <b>{asker.debt} 🍪</b>.",
                parse_mode="HTML",
            )
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🤝 Дать в долг", callback_data=f"aloan_yes_{amount}_{sender_data.user_id}_{target_data.user_id}"),
                InlineKeyboardButton(text="❌ Отказать", callback_data=f"aloan_no_{sender_data.user_id}")
            ]
        ])

        sent_msg = await message.answer(
            f"🙏 {escape_html(sender_data.display_name)} просит у вас <b>{amount} 🍪</b> в долг.\n\n"
            f"Вы готовы выручить игрока?",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        _loan_sessions[f"{message.chat.id}_{sent_msg.message_id}"] = {
            "kind": "request",
            "amount": amount,
            "borrower_id": sender_data.user_id,
            "lender_id": target_data.user_id,
        }

    @router.callback_query(F.data.startswith("aloan_"))
    async def ask_loan_callback(query: CallbackQuery) -> None:
        parts = query.data.split("_")
        action = parts[1]
        session_key = f"{query.message.chat.id}_{query.message.message_id}"
        session = _loan_sessions.get(session_key)
        if not session:
            await query.answer("⏳ Этот запрос уже неактивен.", show_alert=True)
            return
        
        if action == "no":
            original_asker_id = int(parts[2])
            if query.from_user.id != session.get("lender_id"):
                await query.answer("Отказать должен тот, у кого просили.", show_alert=True)
                return
            _loan_sessions.pop(session_key, None)
            await query.message.edit_text("❌ В кредите отказано.")
            return

        # aloan_yes_{amount}_{asker_id}_{lender_id}
        amount = int(parts[2])
        asker_id = int(parts[3])
        lender_id = int(parts[4])
        if (
            session.get("kind") != "request"
            or session.get("amount") != amount
            or session.get("borrower_id") != asker_id
            or session.get("lender_id") != lender_id
        ):
            await query.answer("❌ Кнопка не совпадает с запросом.", show_alert=True)
            return
        
        if query.from_user.id != lender_id:
            await query.answer("Просили не у вас!", show_alert=True)
            return
            
        lender = db.get_user_by_platform_id(query.message.chat.id, lender_id)
        asker = db.get_user_by_platform_id(query.message.chat.id, asker_id)
        
        if not lender or lender.reputation < amount:
            await query.answer(f"❌ У вас недостаточно печенек! Нужно {amount}, а у вас {lender.reputation if lender else 0}.", show_alert=True)
            return
        if not asker:
            await query.message.edit_text("❌ Ошибка: проситель не найден.")
            return
        if asker.debt + amount > _loan_limit(asker):
            await query.message.edit_text("❌ Сделка отменена: у просителя уже слишком большой долг.")
            return
             
        # Выполняем сделку
        _loan_sessions.pop(session_key, None)
        db.update_user(lender.id, {"reputation": lender.reputation - amount})
        db.update_user(asker.id, {
            "reputation": asker.reputation + amount,
            "debt": asker.debt + amount,
            "last_loan_at": datetime.now(timezone.utc).isoformat()
        })
        db.create_debt(lender, asker, amount)
        
        await query.message.edit_text(
            f"🤝 <b>Сделка совершена!</b>\n\n"
            f"{escape_html(lender.display_name)} выручил <b>{amount} 🍪</b> для {escape_html(asker.display_name)}.\n"
            f"⚠️ Долг записан и должен быть возвращен командой <code>/repay</code> в течение 48 часов.",
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

        result = db.repay_debts(user, amount)
        paid = int(result.get("paid") or 0)
        if paid <= 0:
            db.update_user(user.id, {"reputation": user.reputation - amount, "debt": user.debt - amount})
            paid = amount
            payment_lines = ["• кредитор не найден в новой таблице, списан старый общий долг"]
        else:
            payment_lines = [
                f"• {escape_html(p['lender_name'])}: <b>{p['amount']} 🍪</b>"
                for p in result.get("payments", [])
            ]
        
        await message.answer(
            f"💰 <b>Долг погашен!</b>\n\n"
            f"Ты вернул <b>{paid} 🍪</b>.\n"
            f"{chr(10).join(payment_lines)}\n\n"
            f"Остаток долга: <b>{max(0, user.debt - paid)} 🍪</b>.",
            parse_mode="HTML"
        )

    @router.message(Command("debts"))
    async def debts_command(message: Message) -> None:
        sender_data = get_sender_data(message)
        user = db.get_or_create_user(message.chat.id, sender_data)
        borrowed = db.get_active_debts_for_borrower(message.chat.id, user.user_id)
        lent = db.get_active_debts_for_lender(message.chat.id, user.user_id)

        lines = ["📒 <b>Долги</b>", ""]
        if borrowed:
            lines.append("<b>Ты должен:</b>")
            for debt in borrowed[:10]:
                left = int(debt["amount"]) - int(debt.get("paid_amount") or 0) - int(debt.get("forgiven_amount") or 0)
                due_at = _parse_iso_dt(debt.get("due_at"))
                overdue = due_at and due_at < datetime.now(timezone.utc)
                marker = " 🚨 просрочен" if overdue else ""
                lines.append(f"• {escape_html(debt.get('lender_name') or str(debt['lender_id']))}: <b>{left} 🍪</b>{marker}")
        else:
            lines.append("<b>Ты должен:</b> ничего")

        lines.append("")
        if lent:
            lines.append("<b>Тебе должны:</b>")
            for debt in lent[:10]:
                left = int(debt["amount"]) - int(debt.get("paid_amount") or 0) - int(debt.get("forgiven_amount") or 0)
                lines.append(f"• {escape_html(debt.get('borrower_name') or str(debt['borrower_id']))}: <b>{left} 🍪</b>")
        else:
            lines.append("<b>Тебе должны:</b> ничего")
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("forgive"))
    async def forgive_command(message: Message, command: CommandObject) -> None:
        if not message.reply_to_message or not command.args or not command.args.strip().isdigit():
            await message.answer("🕊 <b>Простить долг</b>\n\nИспользование: <code>/forgive &lt;сумма&gt;</code> в ответ должнику.", parse_mode="HTML")
            return
        amount = int(command.args.strip())
        if amount <= 0:
            await message.answer("Сумма должна быть больше нуля.")
            return
        lender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        borrower_id = get_sender_data(message.reply_to_message).user_id
        result = db.forgive_debts(lender, borrower_id, amount)
        forgiven = int(result.get("forgiven") or 0)
        borrower = result.get("borrower")
        if forgiven <= 0 or not borrower:
            await message.answer("❌ Активного долга перед тобой не найдено.")
            return
        await message.answer(
            f"🕊 <b>Долг прощён.</b>\n"
            f"{escape_html(lender.display_name)} простил {escape_html(borrower.display_name)} <b>{forgiven} 🍪</b>.",
            parse_mode="HTML",
        )

    @router.message(Command("jail"))
    async def jail_command(message: Message) -> None:
        target_data = get_sender_data(message.reply_to_message) if message.reply_to_message else get_sender_data(message)
        user = db.get_or_create_user(message.chat.id, target_data)
        remaining = _jail_remaining(user)
        if not remaining:
            await message.answer(f"✅ {escape_html(user.display_name)} на свободе.", parse_mode="HTML")
            return
        await message.answer(
            f"🚔 <b>{escape_html(user.display_name)} в тюрьме</b>\n"
            f"Осталось: <b>{_format_remaining(remaining)}</b>\n"
            f"Причина: <i>{escape_html(user.jail_reason or 'нарушение')}</i>\n"
            f"Залог: <b>{_bail_cost(user)} 🍪</b>",
            parse_mode="HTML",
        )

    @router.message(Command("judge"))
    async def judge_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Судить может только администратор.")
            return
        if not message.reply_to_message or not command.args:
            await message.answer(
                "⚖️ <b>Суд</b>\n\n"
                "Использование:\n"
                "<code>/judge 30 причина</code> — посадить на 30 минут\n"
                "<code>/judge release</code> — выпустить",
                parse_mode="HTML",
            )
            return

        target = db.get_or_create_user(message.chat.id, get_sender_data(message.reply_to_message))
        args = command.args.strip().split(maxsplit=1)
        if args[0].lower() in {"release", "free", "выпустить"}:
            db.update_user(target.id, {"jailed_until": None, "jail_reason": None, "steal_fail_streak": 0})
            await message.answer(f"🔓 {escape_html(target.display_name)} выпущен на свободу.", parse_mode="HTML")
            return
        if not args[0].isdigit():
            await message.answer("❌ Укажи минуты: <code>/judge 30 причина</code>", parse_mode="HTML")
            return
        minutes = min(24 * 60, max(1, int(args[0])))
        reason = args[1] if len(args) > 1 else "решение суда"
        _jail_user(target, minutes, reason)
        await message.answer(
            f"⚖️ <b>Приговор исполнен.</b>\n"
            f"{escape_html(target.display_name)} отправлен в тюрьму на <b>{minutes} мин.</b>\n"
            f"Причина: <i>{escape_html(reason)}</i>",
            parse_mode="HTML",
        )

    @router.message(Command("bail"))
    async def bail_command(message: Message) -> None:
        user = db.get_or_create_user(message.chat.id, get_sender_data(message))
        remaining = _jail_remaining(user)
        if not remaining:
            await message.answer("✅ Ты уже на свободе.")
            return
        cost = _bail_cost(user)
        if user.reputation < cost:
            await message.answer(f"❌ Не хватает на залог. Нужно <b>{cost} 🍪</b>, у тебя <b>{user.reputation} 🍪</b>.", parse_mode="HTML")
            return

        _bail_sessions[f"{message.chat.id}_{user.user_id}"] = {
            "cost": cost,
            "created_at": time.time(),
        }
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=f"✅ Оплатить {cost} 🍪", callback_data=f"bail_pay_{user.user_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"bail_cancel_{user.user_id}"),
            ]
        ])
        await message.answer(
            f"🔐 <b>Подтвердить залог?</b>\n"
            f"Стоимость: <b>{cost} 🍪</b>\n"
            f"Осталось сидеть: <b>{_format_remaining(remaining)}</b>\n\n"
            f"Если есть долг, до половины залога уйдет кредиторам.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    @router.callback_query(F.data.startswith("bail_"))
    async def bail_callback(query: CallbackQuery) -> None:
        if not query.message or not query.data:
            await query.answer("Сообщение недоступно.", show_alert=True)
            return
        parts = query.data.split("_")
        if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
            await query.answer("Кнопка устарела.", show_alert=True)
            return
        action = parts[1]
        if action not in {"pay", "cancel"}:
            await query.answer("Кнопка устарела.", show_alert=True)
            return
        user_id = int(parts[2])
        if query.from_user.id != user_id:
            await query.answer("Это не твой залог.", show_alert=True)
            return

        session_key = f"{query.message.chat.id}_{user_id}"
        session = _bail_sessions.get(session_key)
        if not session or time.time() - session["created_at"] > 300:
            _bail_sessions.pop(session_key, None)
            await query.answer("Залог устарел. Напиши /bail еще раз.", show_alert=True)
            return

        if action == "cancel":
            _bail_sessions.pop(session_key, None)
            await query.message.edit_text("❌ Залог отменен.")
            await query.answer()
            return

        user = db.get_user_by_platform_id(query.message.chat.id, user_id)
        if not user:
            _bail_sessions.pop(session_key, None)
            await query.answer("Профиль не найден.", show_alert=True)
            return
        remaining = _jail_remaining(user)
        if not remaining:
            _bail_sessions.pop(session_key, None)
            await query.message.edit_text("✅ Ты уже на свободе.")
            await query.answer()
            return
        cost = int(session["cost"])
        if user.reputation < cost:
            await query.answer(f"Не хватает печенек: нужно {cost}, у тебя {user.reputation}.", show_alert=True)
            return

        result = _pay_bail(user, cost)
        _bail_sessions.pop(session_key, None)
        await query.message.edit_text(
            f"🔓 <b>Залог оплачен.</b>\n"
            f"Стоимость: <b>{cost} 🍪</b>\n"
            f"Кредиторам ушло: <b>{result['creditor_part']} 🍪</b>\n"
            f"Ты снова на свободе.",
            parse_mode="HTML",
        )
        await query.answer()

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

        sender = db.get_or_create_user(message.chat.id, sender_data)
        target = db.get_or_create_user(message.chat.id, target_data)
        if await _deny_if_jailed(message, sender, "/steal"):
            return
        
        if target.reputation < 10:
            await message.answer("🥥 У цели слишком мало печенек. Ниже 10 🍪 воровать нельзя.")
            return

        ok, remaining = db.can_user_use_command(message.chat.id, sender_data.user_id, "steal", 900)
        if not ok:
            await message.answer(f"⏳ Ты ещё не отошёл от прошлого дела. Подожди {remaining} сек.")
            return

        level_delta = sender.level - target.level
        success_chance = min(0.45, max(0.15, 0.30 + level_delta * 0.02 + sender.steal_success_streak * 0.01))
        roll = random.random()

        if roll < success_chance:
            critical = roll < 0.04
            max_steal = min(25, max(1, int(target.reputation * 0.08)))
            amount = random.randint(1, max_steal)
            if critical:
                amount = min(target.reputation, amount * 2)
            db.update_user(target.id, {"reputation": max(0, target.reputation - amount)})
            db.update_user(
                sender.id,
                {
                    "reputation": sender.reputation + amount,
                    "steal_success_streak": min(sender.steal_success_streak + 1, 5),
                    "steal_fail_streak": 0,
                },
            )
            crit_text = "\n✨ <i>Критический успех!</i>" if critical else ""
            await message.answer(
                f"🤫 <b>УСПЕХ!</b> {escape_html(sender.display_name)} стащил <b>{amount} 🍪</b> "
                f"у {escape_html(target.display_name)}.{crit_text}",
                parse_mode="HTML",
            )
            return

        fail_streak = sender.steal_fail_streak + 1
        critical_fail = roll > 0.94
        penalty = min(sender.reputation, max(2, int(max(sender.reputation, 20) * (0.12 if critical_fail else 0.06))))
        jail_chance = 0.2 + (0.15 if fail_streak >= 3 else 0) + (0.25 if critical_fail else 0)
        jailed = random.random() < jail_chance

        updates = {
            "reputation": max(0, sender.reputation - penalty),
            "steal_fail_streak": fail_streak,
            "steal_success_streak": 0,
        }
        if jailed:
            jail_minutes = 180 if critical_fail else (90 if fail_streak >= 3 else 45)
            updates["jailed_until"] = (datetime.now(timezone.utc) + timedelta(minutes=jail_minutes)).isoformat()
            updates["jail_reason"] = "провал кражи"
        db.update_user(sender.id, updates)

        jail_text = f"\n🚔 И ещё тебя посадили на <b>{jail_minutes} мин.</b>" if jailed else ""
        await message.answer(
            f"🤡 <b>ТЕБЯ ПОЙМАЛИ!</b> {escape_html(sender.display_name)} полез к "
            f"{escape_html(target.display_name)} и потерял <b>{penalty} 🍪</b>.{jail_text}",
            parse_mode="HTML",
        )

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
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Эта магия доступна только администраторам.")
            return
        
        users = db.get_active_users(message.chat.id, minutes=24 * 60, limit=100)
        if not users:
            await message.answer("🌧 Некого поливать: за сутки активных участников не найдено.")
            return
        
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
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("🤫 Тсс... (Только для админов)")
            return
        if not command.args: return
        
        await message.delete()
        await message.answer(command.args)

    _drop_sessions = AUTO_DROP_SESSIONS
    _quiz_sessions = AUTO_QUIZ_SESSIONS

    def _callback_sender(query: CallbackQuery) -> Sender:
        user = query.from_user
        return Sender(
            user_id=user.id,
            first_name=user.first_name or "Инкогнито",
            username=user.username,
            is_bot=user.is_bot,
        )

    def _is_group_chat(message: Message) -> bool:
        chat_type = getattr(message.chat, "type", "")
        return getattr(chat_type, "value", str(chat_type)) in {"group", "supergroup"}

    def _parse_bet_and_choice(args: str | None) -> tuple[int | None, str | None]:
        if not args:
            return None, None
        parts = args.strip().lower().split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            return None, None
        return int(parts[0]), parts[1].strip()

    def _max_game_bet(user) -> int:
        if user.reputation <= 0:
            return 0
        balance_cap = max(1, int(user.reputation * 0.35))
        return min(user.reputation, 5000, balance_cap)

    async def _deny_bad_bet(message: Message, user, bet: int, *, min_bet: int = 1) -> bool:
        if bet < min_bet:
            await message.answer(f"❌ Минимальная ставка: <b>{min_bet}</b> 🍪", parse_mode="HTML")
            return True
        max_bet = _max_game_bet(user)
        if max_bet < min_bet:
            await message.answer(
                f"❌ Для этой игры нужно хотя бы <b>{min_bet}</b> 🍪.\n"
                f"Твой безопасный лимит сейчас: <b>{max_bet}</b> 🍪",
                parse_mode="HTML",
            )
            return True
        if bet > max_bet:
            await message.answer(
                f"❌ Слишком большая ставка для здоровой экономики.\n"
                f"Твой лимит сейчас: <b>{max_bet}</b> 🍪",
                parse_mode="HTML",
            )
            return True
        return False

    @router.message(Command("coin", "flip", "монетка"))
    async def coin_command(message: Message, command: CommandObject) -> None:
        bet, choice = _parse_bet_and_choice(command.args)
        aliases = {
            "орел": "heads",
            "орёл": "heads",
            "o": "heads",
            "heads": "heads",
            "head": "heads",
            "h": "heads",
            "решка": "tails",
            "tails": "tails",
            "tail": "tails",
            "t": "tails",
        }
        if not bet or bet <= 0 or choice not in aliases:
            await message.answer(
                "🪙 <b>Монетка</b>\n\n"
                "Использование: <code>/coin &lt;ставка&gt; &lt;орел/решка&gt;</code>\n"
                "Победа дает x1.9. Простая риск-игра, не фармилка.",
                parse_mode="HTML",
            )
            return

        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await _deny_if_jailed(message, sender, "/coin"):
            return
        if await _deny_bad_bet(message, sender, bet):
            return
        if sender.reputation < bet:
            await message.answer(f"❌ Не хватает печенек. Баланс: <b>{sender.reputation}</b> 🍪", parse_mode="HTML")
            return
        allowed, remaining = db.can_user_use_command(message.chat.id, sender.user_id, "coin", 20)
        if not allowed:
            await message.answer(f"⏳ Монетка еще крутится. Подожди {remaining} сек.")
            return

        player_side = aliases[choice]
        result_side = player_side if random.random() < 0.48 else ("tails" if player_side == "heads" else "heads")
        result_text = "орел" if result_side == "heads" else "решка"
        if player_side == result_side:
            win = int(bet * 1.9)
            new_balance = sender.reputation - bet + win
            db.update_user(sender.id, {"reputation": new_balance})
            await message.answer(
                f"🪙 Выпало: <b>{result_text}</b>\n"
                f"✅ {escape_html(sender.display_name)} выиграл <b>{win}</b> 🍪\n"
                f"Баланс: <b>{new_balance}</b> 🍪",
                parse_mode="HTML",
            )
        else:
            new_balance = sender.reputation - bet
            db.update_user(sender.id, {"reputation": new_balance})
            await message.answer(
                f"🪙 Выпало: <b>{result_text}</b>\n"
                f"❌ Ставка <b>{bet}</b> 🍪 сгорела.\n"
                f"Баланс: <b>{new_balance}</b> 🍪",
                parse_mode="HTML",
            )

    @router.message(Command("rps", "кнб"))
    async def rps_command(message: Message, command: CommandObject) -> None:
        bet, choice = _parse_bet_and_choice(command.args)
        aliases = {
            "камень": "rock",
            "rock": "rock",
            "r": "rock",
            "ножницы": "scissors",
            "scissors": "scissors",
            "s": "scissors",
            "бумага": "paper",
            "paper": "paper",
            "p": "paper",
        }
        labels = {"rock": "камень", "scissors": "ножницы", "paper": "бумага"}
        beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
        if not bet or bet <= 0 or choice not in aliases:
            await message.answer(
                "✊ <b>Камень-ножницы-бумага</b>\n\n"
                "Использование: <code>/rps &lt;ставка&gt; &lt;камень/ножницы/бумага&gt;</code>\n"
                "Победа дает x1.9, ничья возвращает ставку.",
                parse_mode="HTML",
            )
            return

        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await _deny_if_jailed(message, sender, "/rps"):
            return
        if await _deny_bad_bet(message, sender, bet):
            return
        if sender.reputation < bet:
            await message.answer(f"❌ Не хватает печенек. Баланс: <b>{sender.reputation}</b> 🍪", parse_mode="HTML")
            return
        allowed, remaining = db.can_user_use_command(message.chat.id, sender.user_id, "rps", 20)
        if not allowed:
            await message.answer(f"⏳ Подожди {remaining} сек. перед новой дуэлью.")
            return

        player = aliases[choice]
        roll = random.random()
        if roll < 0.34:
            bot_choice = beats[player]
        elif roll < 0.62:
            bot_choice = player
        else:
            bot_choice = next(symbol for symbol, beaten in beats.items() if beaten == player)
        if player == bot_choice:
            await message.answer(
                f"🤝 Ничья.\nТы: <b>{labels[player]}</b>, бот: <b>{labels[bot_choice]}</b>\n"
                f"Ставка возвращена.",
                parse_mode="HTML",
            )
        elif beats[player] == bot_choice:
            win = int(bet * 1.9)
            new_balance = sender.reputation - bet + win
            db.update_user(sender.id, {"reputation": new_balance})
            await message.answer(
                f"✊ <b>Победа!</b>\nТы: <b>{labels[player]}</b>, бот: <b>{labels[bot_choice]}</b>\n"
                f"Выигрыш: <b>{win}</b> 🍪\nБаланс: <b>{new_balance}</b> 🍪",
                parse_mode="HTML",
            )
        else:
            new_balance = sender.reputation - bet
            db.update_user(sender.id, {"reputation": new_balance})
            await message.answer(
                f"💥 <b>Проигрыш.</b>\nТы: <b>{labels[player]}</b>, бот: <b>{labels[bot_choice]}</b>\n"
                f"Потеряно: <b>{bet}</b> 🍪\nБаланс: <b>{new_balance}</b> 🍪",
                parse_mode="HTML",
            )

    @router.message(Command("duel", "дуэль"))
    async def duel_command(message: Message, command: CommandObject) -> None:
        bet, choice = _parse_bet_and_choice(command.args)
        aliases = {
            "камень": "rock",
            "rock": "rock",
            "r": "rock",
            "ножницы": "scissors",
            "scissors": "scissors",
            "s": "scissors",
            "бумага": "paper",
            "paper": "paper",
            "p": "paper",
        }
        labels = {"rock": "камень", "scissors": "ножницы", "paper": "бумага"}
        if not message.reply_to_message or not message.reply_to_message.from_user:
            await message.answer(
                "⚔️ <b>Дуэль с игроком</b>\n\n"
                "Ответь на сообщение соперника: <code>/duel сумма камень</code>\n"
                "Можно выбрать: камень, ножницы, бумага. Соперник подтвердит дуэль кнопкой.",
                parse_mode="HTML",
            )
            return
        if not bet or bet <= 0 or choice not in aliases:
            await message.answer("Использование: ответом на игрока <code>/duel сумма камень/ножницы/бумага</code>", parse_mode="HTML")
            return

        target_sender = get_sender_data(message.reply_to_message)
        if target_sender.is_bot or target_sender.user_id == message.from_user.id:
            await message.answer("❌ Дуэль нужна между двумя живыми игроками.")
            return

        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        target = db.get_or_create_user(message.chat.id, target_sender)
        if await _deny_if_jailed(message, sender, "/duel"):
            return
        if _jail_remaining(target):
            await message.answer("❌ Соперник сейчас в тюрьме и не может принять дуэль.")
            return
        if await _deny_bad_bet(message, sender, bet):
            return
        target_max_bet = _max_game_bet(target)
        if bet > target_max_bet:
            await message.answer(
                f"❌ Для соперника ставка слишком большая.\n"
                f"Его лимит сейчас: <b>{target_max_bet}</b> 🍪",
                parse_mode="HTML",
            )
            return
        if sender.reputation < bet:
            await message.answer(f"❌ Не хватает печенек. Баланс: <b>{sender.reputation}</b> 🍪", parse_mode="HTML")
            return

        key = f"{abs(message.chat.id) % 1_000_000}{sender.user_id % 1_000_000}{target.user_id % 1_000_000}{random.randint(100, 999)}"
        DUEL_SESSIONS[key] = {
            "chat_id": message.chat.id,
            "challenger_id": sender.user_id,
            "target_id": target.user_id,
            "bet": bet,
            "choice": aliases[choice],
            "created_at": time.time(),
        }
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🪨 Камень", callback_data=f"duel_{key}_rock"),
                InlineKeyboardButton(text="✂️ Ножницы", callback_data=f"duel_{key}_scissors"),
                InlineKeyboardButton(text="📄 Бумага", callback_data=f"duel_{key}_paper"),
            ],
            [InlineKeyboardButton(text="Отказаться", callback_data=f"duel_{key}_decline")],
        ])
        await message.answer(
            f"⚔️ <b>Дуэль на {bet} 🍪</b>\n\n"
            f"{escape_html(sender.display_name)} бросает вызов {escape_html(target.display_name)}.\n"
            f"Выбор вызывающего скрыт. У соперника 15 минут на ответ.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("duel_"))
    async def duel_callback(query: CallbackQuery) -> None:
        parts = (query.data or "").split("_", 2)
        if len(parts) != 3:
            await query.answer("Дуэль устарела.", show_alert=True)
            return
        key, target_choice = parts[1], parts[2]
        session = DUEL_SESSIONS.get(key)
        if not session or time.time() - session["created_at"] > 15 * 60:
            DUEL_SESSIONS.pop(key, None)
            await query.answer("Дуэль устарела.", show_alert=True)
            return
        if query.from_user.id != session["target_id"]:
            await query.answer("Эта дуэль не тебе.", show_alert=True)
            return
        if target_choice == "decline":
            DUEL_SESSIONS.pop(key, None)
            await query.message.edit_text("⚔️ Дуэль отклонена.")
            await query.answer()
            return

        challenger = db.get_user_by_platform_id(session["chat_id"], session["challenger_id"])
        target = db.get_user_by_platform_id(session["chat_id"], session["target_id"])
        if not challenger or not target:
            DUEL_SESSIONS.pop(key, None)
            await query.answer("Не вижу одного из игроков в базе.", show_alert=True)
            return
        if _jail_remaining(challenger) or _jail_remaining(target):
            DUEL_SESSIONS.pop(key, None)
            await query.message.edit_text("⚔️ Дуэль отменена: один из игроков сейчас в тюрьме.")
            await query.answer()
            return
        bet = int(session["bet"])
        if challenger.reputation < bet or target.reputation < bet:
            DUEL_SESSIONS.pop(key, None)
            await query.message.edit_text("⚔️ Дуэль отменена: у одного из игроков уже не хватает печенек.")
            await query.answer()
            return

        labels = {"rock": "камень", "scissors": "ножницы", "paper": "бумага"}
        beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
        challenger_choice = session["choice"]
        DUEL_SESSIONS.pop(key, None)
        if challenger_choice == target_choice:
            await query.message.edit_text(
                f"🤝 <b>Ничья!</b>\n"
                f"{escape_html(challenger.display_name)}: <b>{labels[challenger_choice]}</b>\n"
                f"{escape_html(target.display_name)}: <b>{labels[target_choice]}</b>\n"
                "Печеньки остаются на месте.",
                parse_mode="HTML",
            )
            await query.answer()
            return

        if beats[challenger_choice] == target_choice:
            winner, loser = challenger, target
        else:
            winner, loser = target, challenger
        loser_refund = max(1, int(bet * 0.15)) if bet >= 7 else 0
        winner_profit = bet - loser_refund
        db.update_user(winner.id, {"reputation": winner.reputation + winner_profit})
        db.update_user(loser.id, {"reputation": loser.reputation - bet + loser_refund})
        await query.message.edit_text(
            f"⚔️ <b>Дуэль завершена</b>\n"
            f"{escape_html(challenger.display_name)}: <b>{labels[challenger_choice]}</b>\n"
            f"{escape_html(target.display_name)}: <b>{labels[target_choice]}</b>\n\n"
            f"Победитель: <b>{escape_html(winner.display_name)}</b>\n"
            f"Выигрыш: <b>{winner_profit}</b> 🍪\n"
            f"Утешение проигравшему: <b>{loser_refund}</b> 🍪",
            parse_mode="HTML",
        )
        await query.answer()

    async def _charge_after_success(user_id: int, chat_id: int, price: int) -> tuple[bool, int]:
        fresh = db.get_user_by_platform_id(chat_id, user_id)
        if not fresh or fresh.reputation < price:
            return False, fresh.reputation if fresh else 0
        updated = db.update_user(fresh.id, {"reputation": fresh.reputation - price})
        return bool(updated), (updated.reputation if updated else fresh.reputation - price)

    def _reference_image_spec(message: Message) -> tuple[str, str] | None:
        if message.photo:
            return message.photo[-1].file_id, "image/jpeg"
        if message.sticker and not message.sticker.is_animated and not message.sticker.is_video:
            return message.sticker.file_id, "image/webp"
        if message.document and (message.document.mime_type or "").startswith("image/"):
            return message.document.file_id, message.document.mime_type or "image/jpeg"
        return None

    async def _download_reference_images(message: Message, *, max_images: int = 2) -> list[tuple[bytes, str]]:
        specs: list[tuple[str, str]] = []
        current = _reference_image_spec(message)
        if current:
            specs.append(current)
        if message.reply_to_message:
            replied = _reference_image_spec(message.reply_to_message)
            if replied:
                specs.append(replied)

        refs: list[tuple[bytes, str]] = []
        for file_id, mime_type in specs[:max_images]:
            try:
                file_info = await message.bot.get_file(file_id)
                if file_info.file_size and file_info.file_size > ai.settings.ai_vision_max_bytes:
                    continue
                buffer = BytesIO()
                await message.bot.download_file(file_info.file_path, destination=buffer)
                payload = buffer.getvalue()
                if payload and len(payload) <= ai.settings.ai_vision_max_bytes:
                    refs.append((payload, mime_type))
            except Exception as exc:
                print(f"[AI:image_reference_download_error] error={exc}")
        return refs

    async def _download_single_reference_image(message: Message) -> tuple[bytes, str] | None:
        spec = _reference_image_spec(message)
        if not spec:
            return None
        file_id, mime_type = spec
        try:
            file_info = await message.bot.get_file(file_id)
            if file_info.file_size and file_info.file_size > ai.settings.ai_vision_max_bytes:
                return None
            buffer = BytesIO()
            await message.bot.download_file(file_info.file_path, destination=buffer)
            payload = buffer.getvalue()
            if not payload or len(payload) > ai.settings.ai_vision_max_bytes:
                return None
            return payload, mime_type
        except Exception as exc:
            print(f"[AI:nika_reference_download_error] error={exc}")
            return None

    def _load_saved_nika_reference() -> tuple[bytes, str] | None:
        asset = db.get_bot_asset(NIKA_REFERENCE_ASSET_KEY)
        if not asset:
            return None
        try:
            payload = base64.b64decode(str(asset.get("payload_base64") or ""))
        except Exception:
            return None
        if not payload:
            return None
        return payload, str(asset.get("mime_type") or "image/png")

    def _reference_caption(has_identity: bool, has_pose: bool) -> str:
        parts = []
        if has_identity:
            parts.append("внешность Ники взята из сохраненного референса")
        if has_pose:
            parts.append("reply-картинка использована для позы/ракурса")
        return "\n" + "; ".join(parts) + "." if parts else ""

    def _nika_character_prompt() -> str:
        return (
            "NeuroNika, an original anime cyberpunk punk girl mascot: long vivid electric-blue hair with purple glow, "
            "violet eyes, black glossy leather punk outfit, black cat-ear hood with small skull details, chains, belts, "
            "purple neon accents, confident playful expression. Keep her recognizable across images, but do not copy any "
            "specific existing character or real person."
        )

    def _random_nika_scene() -> tuple[str, str, str]:
        places = [
            "on a rainy neon rooftop at night",
            "inside a cozy streamer room with purple LED light",
            "in a futuristic arcade full of slot machines",
            "near a glowing vending machine in a cyberpunk alley",
            "sitting on a motorcycle under blue and violet neon",
            "at a small cafe table with cookies and a phone",
            "in a music studio with black acoustic panels",
            "on a train platform with holographic ads",
            "in a bedroom mirror selfie setup with soft neon",
            "at a graffiti wall with purple paint splashes",
        ]
        poses = [
            "holding the sign with both hands close to the camera",
            "leaning sideways and showing the sign on a clipboard",
            "sitting cross-legged while holding a small card",
            "winking and pointing at the handwritten sign",
            "standing full-body with one boot forward and the sign in one hand",
            "taking a mirror selfie while the sign is visible",
            "resting one elbow on a table and sliding the sign toward the viewer",
            "crouching in a streetwear pose with the sign hanging from a chain clip",
            "holding a polaroid-style sign near her face",
            "pinning the sign to a neon-lit board behind her",
        ]
        cameras = [
            "portrait composition, crisp readable text",
            "dynamic three-quarter view, high detail",
            "medium shot, cinematic lighting",
            "full-body fashion illustration, readable sign",
            "close-up with shallow depth of field, sign fully visible",
        ]
        return random.choice(places), random.choice(poses), random.choice(cameras)

    def _wants_nika_in_image(prompt: str) -> bool:
        lowered = prompt.lower()
        markers = ("ника", "нейроника", "nika", "neuronika", "с ней", "с тобой", "с никой", "ты на")
        return any(marker in lowered for marker in markers)

    def _build_ai_image_prompt(user_prompt: str) -> str:
        place, pose, camera = _random_nika_scene()
        nika_part = ""
        if _wants_nika_in_image(user_prompt):
            nika_part = (
                f"Include {_nika_character_prompt()} She is {pose} {place}. "
                "Vary the pose, outfit details, camera angle and location from previous generations. "
            )
        reference_part = (
            "If a reference image is provided, use it only as a pose/composition/environment reference: "
            "borrow the body pose, camera angle, framing, gesture or location mood. "
            "Do not borrow the person's identity, face, hair, outfit or exact appearance from the reference. "
        )
        return (
            "Create a high quality Telegram reward image. "
            f"{reference_part}"
            f"{nika_part}"
            "Use rich composition, strong lighting, no watermark, no fake UI, no unreadable random text. "
            f"Camera/style: {camera}. "
            f"User request: {user_prompt[:1000]}"
        )

    def _build_ai_sign_prompt(sign_text: str) -> str:
        place, pose, camera = _random_nika_scene()
        return (
            "Create an AI signa image for a Telegram economy bot. "
            f"Main character: {_nika_character_prompt()} "
            "If multiple reference images are provided: the first reference is NeuroNika's identity reference and must define her face, hair, outfit, color palette and anime style; "
            "any later reference image is only for pose, gesture, camera angle, scene layout or location mood. "
            "NeuroNika's identity must always stay the same: electric-blue/purple hair, violet eyes, black punk cyber outfit, cat-ear hood, neon purple accents. "
            "Never copy identity, face, hair, body, clothes or exact appearance from pose-only references. "
            f"Scene: she is {pose} {place}. "
            "The sign must be a physical paper/card/phone note naturally held by NeuroNika, not a floating caption. "
            f"The sign text must be clearly readable and exactly: {sign_text[:120]!r}. "
            "Every generation should feel different: vary pose, background, props, framing, facial expression and lighting. "
            f"Camera/style: {camera}. "
            "No watermark, no extra random letters, no fake app interface, no real person likeness."
        )

    @router.message(Command("setnika"))
    async def set_nika_reference_command(message: Message) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Менять референс Ники может только админ.")
            return
        source = message.reply_to_message or message
        image = await _download_single_reference_image(source)
        if not image:
            await message.answer(
                "Ответь командой <code>/setnika</code> на картинку/стикер/изображение-документ Ники.",
                parse_mode="HTML",
            )
            return
        payload, mime_type = image
        ok = db.save_bot_asset(
            NIKA_REFERENCE_ASSET_KEY,
            base64.b64encode(payload).decode("ascii"),
            mime_type,
            updated_by=sender.user_id,
        )
        if not ok:
            await message.answer("❌ Не смогла сохранить референс. Проверь SQL bot_assets в signs_and_ai_images.sql.")
            return
        await message.answer(
            f"✅ Референс Ники сохранен.\n"
            f"Тип: <code>{escape_html(mime_type)}</code>\n"
            f"Размер: <b>{len(payload) // 1024}</b> KB\n\n"
            "Теперь /signai всегда берет внешность из него, а reply-картинка нужна только для позы/ракурса.",
            parse_mode="HTML",
        )

    @router.message(Command("nika"))
    async def nika_reference_status_command(message: Message) -> None:
        asset = db.get_bot_asset(NIKA_REFERENCE_ASSET_KEY)
        if not asset:
            await message.answer("Референс Ники еще не сохранен. Админ может ответить на картинку командой <code>/setnika</code>.", parse_mode="HTML")
            return
        payload_size = len(str(asset.get("payload_base64") or "")) * 3 // 4
        await message.answer(
            "🖼️ <b>Референс Ники сохранен</b>\n\n"
            f"Тип: <code>{escape_html(str(asset.get('mime_type') or 'unknown'))}</code>\n"
            f"Размер: <b>{payload_size // 1024}</b> KB\n"
            f"Обновлен: <code>{escape_html(str(asset.get('updated_at') or 'unknown'))}</code>",
            parse_mode="HTML",
        )

    @router.message(Command("aiimage", "картинка"))
    async def ai_image_command(message: Message, command: CommandObject) -> None:
        prompt = (command.args or "").strip()
        if not prompt:
            await message.answer(
                "🖼️ <b>ИИ-картинка</b>\n\n"
                f"Цена: <b>{ai.settings.ai_image_price}</b> 🍪\n"
                "Использование: <code>/aiimage описание картинки</code>\n"
                "Если ответить командой на картинку, она станет референсом позы/ракурса, а не внешности.",
                parse_mode="HTML",
            )
            return
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await _deny_if_jailed(message, sender, "/aiimage"):
            return
        price = max(1, ai.settings.ai_image_price)
        if sender.reputation < price:
            await message.answer(f"❌ Для ИИ-картинки нужно <b>{price}</b> 🍪. Баланс: <b>{sender.reputation}</b> 🍪", parse_mode="HTML")
            return
        try:
            await message.bot.send_chat_action(message.chat.id, "upload_photo")
        except Exception:
            pass
        saved_nika_reference = _load_saved_nika_reference()
        pose_references = await _download_reference_images(message)
        reference_images = []
        if saved_nika_reference and _wants_nika_in_image(prompt):
            reference_images.append(saved_nika_reference)
        reference_images.extend(pose_references)
        image_bytes = await ai.generate_image(_build_ai_image_prompt(prompt), reference_images=reference_images)
        if not image_bytes:
            await message.answer("❌ Не получилось сгенерировать картинку. Печеньки не списаны.")
            return
        ok, balance = await _charge_after_success(sender.user_id, message.chat.id, price)
        if not ok:
            await message.answer("❌ Пока картинка генерировалась, печенек уже не хватило. Картинку не списываю.")
            return
        await message.answer_photo(
            BufferedInputFile(image_bytes, filename="nika_ai_image.png"),
            caption=f"🖼️ Готово. Списано <b>{price}</b> 🍪\nБаланс: <b>{balance}</b> 🍪",
            parse_mode="HTML",
        )

    @router.message(Command("signai", "aisign", "сигнаии"))
    async def ai_sign_command(message: Message, command: CommandObject) -> None:
        text = (command.args or "").strip()
        if not text:
            await message.answer(
                "✍️ <b>ИИ-сигна</b>\n\n"
                f"Цена: <b>{ai.settings.ai_sign_price}</b> 🍪\n"
                "Использование: <code>/signai текст на сигне</code>\n"
                "На картинке будет Ника, но сцена/поза/место каждый раз меняются.\n"
                "Внешность берется из сохраненного /setnika, а reply-картинка дает только позу/ракурс.",
                parse_mode="HTML",
            )
            return
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await _deny_if_jailed(message, sender, "/signai"):
            return
        price = max(1, ai.settings.ai_sign_price)
        if sender.reputation < price:
            await message.answer(f"❌ Для ИИ-сигны нужно <b>{price}</b> 🍪. Баланс: <b>{sender.reputation}</b> 🍪", parse_mode="HTML")
            return
        try:
            await message.bot.send_chat_action(message.chat.id, "upload_photo")
        except Exception:
            pass
        safe_text = text[:120]
        saved_nika_reference = _load_saved_nika_reference()
        pose_references = await _download_reference_images(message)
        reference_images = []
        if saved_nika_reference:
            reference_images.append(saved_nika_reference)
        reference_images.extend(pose_references)
        image_bytes = await ai.generate_image(_build_ai_sign_prompt(safe_text), reference_images=reference_images)
        if not image_bytes:
            await message.answer("❌ Не получилось сгенерировать ИИ-сигну. Печеньки не списаны.")
            return
        ok, balance = await _charge_after_success(sender.user_id, message.chat.id, price)
        if not ok:
            await message.answer("❌ Пока сигна генерировалась, печенек уже не хватило. Ничего не списано.")
            return
        await message.answer_photo(
            BufferedInputFile(image_bytes, filename="nika_ai_sign.png"),
            caption=(
                f"✍️ ИИ-сигна готова. Списано <b>{price}</b> 🍪\n"
                f"Баланс: <b>{balance}</b> 🍪"
                + _reference_caption(bool(saved_nika_reference), bool(pose_references))
            ),
            parse_mode="HTML",
        )

    @router.message(Command("setsignprice", "signprice_set"))
    async def set_sign_price_command(message: Message, command: CommandObject) -> None:
        raw_arg = (command.args or "").strip()
        arg = raw_arg.lower()
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if arg in {"off", "0", "выкл"}:
            db.set_sign_price(sender, 0)
            await message.answer("✍️ Цена сигны отключена.")
            return

        def _parse_sign_option(value: str) -> tuple[str, int, str] | None:
            if "|" in value:
                parts = [part.strip() for part in value.split("|")]
                if len(parts) >= 2 and parts[1].isdigit():
                    return parts[0], int(parts[1]), parts[2] if len(parts) >= 3 else ""
                return None
            parts = value.split()
            price_index = next((idx for idx, part in enumerate(parts) if part.isdigit()), None)
            if price_index is None or price_index == 0:
                return None
            title = " ".join(parts[:price_index]).strip()
            amount = int(parts[price_index])
            description = " ".join(parts[price_index + 1 :]).strip()
            return title, amount, description

        if "|" in raw_arg:
            parsed_option = _parse_sign_option(raw_arg)
            if not parsed_option:
                await message.answer(
                    "Формат тарифа: <code>/setsignprice название цена описание</code>\n"
                    "Пример: <code>/setsignprice срочная 900 сделаю сегодня</code>",
                    parse_mode="HTML",
                )
                return
            title, amount, description = parsed_option
            if len(title) < 2:
                await message.answer("❌ Название тарифа слишком короткое.")
                return
            if amount < ai.settings.human_sign_min_price:
                await message.answer(f"❌ Минимальная цена сигны: <b>{ai.settings.human_sign_min_price}</b> 🍪", parse_mode="HTML")
                return
            option = db.create_sign_price_option(sender, title, amount, description)
            if not option:
                await message.answer("❌ Не смог сохранить тариф. Проверь SQL sign_price_options.")
                return
            await message.answer(
                f"✅ Тариф добавлен: <b>#{option['id']} {escape_html(title)}</b> — <b>{amount}</b> 🍪"
                + (f"\n<i>{escape_html(description)}</i>" if description else ""),
                parse_mode="HTML",
            )
            return

        if not arg.isdigit():
            parsed_option = _parse_sign_option(raw_arg)
            if not parsed_option:
                await message.answer(
                    "Использование:\n"
                    f"• базовая цена: <code>/setsignprice сумма</code>\n"
                    "• тариф: <code>/setsignprice название цена описание</code>\n"
                    "Пример: <code>/setsignprice срочная 900 сделаю сегодня</code>\n"
                    f"Минимум: <b>{ai.settings.human_sign_min_price}</b> 🍪",
                    parse_mode="HTML",
                )
                return
            title, amount, description = parsed_option
            if len(title) < 2:
                await message.answer("❌ Название тарифа слишком короткое.")
                return
            if amount < ai.settings.human_sign_min_price:
                await message.answer(f"❌ Минимальная цена сигны: <b>{ai.settings.human_sign_min_price}</b> 🍪", parse_mode="HTML")
                return
            option = db.create_sign_price_option(sender, title, amount, description)
            if not option:
                await message.answer("❌ Не смог сохранить тариф. Проверь SQL sign_price_options.")
                return
            await message.answer(
                f"✅ Тариф добавлен: <b>#{option['id']} {escape_html(title)}</b> — <b>{amount}</b> 🍪"
                + (f"\n<i>{escape_html(description)}</i>" if description else ""),
                parse_mode="HTML",
            )
            return
        amount = int(arg)
        if amount < ai.settings.human_sign_min_price:
            await message.answer(f"❌ Минимальная цена сигны: <b>{ai.settings.human_sign_min_price}</b> 🍪", parse_mode="HTML")
            return
        updated = db.set_sign_price(sender, amount)
        if not updated:
            await message.answer("❌ Не смог сохранить цену. Проверь, применена ли миграция signs_and_ai_images.sql.")
            return
        await message.answer(f"✍️ Твоя базовая цена за сигну: <b>{amount}</b> 🍪", parse_mode="HTML")

    @router.message(Command("delsignprice", "signprice_del"))
    async def delete_sign_price_command(message: Message, command: CommandObject) -> None:
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.answer("Использование: <code>/delsignprice номер_тарифа</code>", parse_mode="HTML")
            return
        ok = db.disable_sign_price_option(message.chat.id, sender.user_id, int(arg))
        await message.answer("✅ Тариф отключен." if ok else "❌ Не нашла такой твой активный тариф.")

    @router.message(Command("signprice", "signprices"))
    async def sign_price_command(message: Message) -> None:
        target_message = message.reply_to_message if message.reply_to_message and message.reply_to_message.from_user else message
        target = db.get_or_create_user(message.chat.id, get_sender_data(target_message))
        price = target.sign_price or ai.settings.human_sign_min_price
        options = db.list_sign_price_options(message.chat.id, target.user_id)
        lines = [
            f"✍️ <b>Цены сигн у {escape_html(target.display_name)}</b>",
            "",
            f"Базовая: <b>{price}</b> 🍪",
        ]
        if options:
            lines.append("")
            lines.append("<b>Тарифы:</b>")
            for option in options:
                description = str(option.get("description") or "").strip()
                lines.append(
                    f"#{option['id']} — <b>{escape_html(str(option.get('title') or 'сигна'))}</b>: "
                    f"<b>{int(option.get('price') or 0)}</b> 🍪"
                    + (f"\n<i>{escape_html(description)}</i>" if description else "")
                )
            lines.append("")
            lines.append("Заказ по тарифу: ответь автору <code>/signreq # текст</code>")
        else:
            lines.append("Отдельных тарифов пока нет.")
        await message.answer("\n".join(lines), parse_mode="HTML")

    def _sign_status_label(status: str) -> str:
        return {
            "pending": "ждет автора",
            "accepted": "в работе",
            "delivered": "ждет подтверждения",
            "paid": "оплачен",
            "cancelled": "отменен",
        }.get(status, status)

    def _format_sign_order(row: dict, *, role: str) -> str:
        other = row.get("buyer_name") if role == "author" else row.get("author_name")
        other_label = "Покупатель" if role == "author" else "Автор"
        option_line = f"Тариф: <b>{escape_html(str(row.get('option_title')))}</b>\n" if row.get("option_title") else ""
        return (
            f"#{row.get('id')} | <b>{_sign_status_label(str(row.get('status')))}</b> | "
            f"<b>{int(row.get('price') or 0)}</b> 🍪\n"
            f"{other_label}: {escape_html(str(other or 'unknown'))}\n"
            f"{option_line}"
            f"Текст: <i>{escape_html(str(row.get('text') or '')[:120])}</i>"
        )

    def _sign_order_keyboard(rows: list[dict], *, viewer_id: int) -> InlineKeyboardMarkup | None:
        buttons = []
        for row in rows:
            order_id = int(row.get("id") or 0)
            status = row.get("status")
            if status == "pending" and int(row.get("author_id") or 0) == viewer_id:
                buttons.append([
                    InlineKeyboardButton(text=f"Принять #{order_id}", callback_data=f"sign_accept_{order_id}"),
                    InlineKeyboardButton(text=f"Отказать #{order_id}", callback_data=f"sign_decline_{order_id}"),
                ])
            elif status == "accepted" and int(row.get("author_id") or 0) == viewer_id:
                buttons.append([
                    InlineKeyboardButton(text=f"Готово #{order_id}", callback_data=f"sign_done_{order_id}"),
                    InlineKeyboardButton(text=f"Отменить #{order_id}", callback_data=f"sign_cancel_{order_id}"),
                ])
            elif status == "delivered" and int(row.get("buyer_id") or 0) == viewer_id:
                buttons.append([InlineKeyboardButton(text=f"Подтвердить оплату #{order_id}", callback_data=f"sign_pay_{order_id}")])
        if not buttons:
            return None
        return InlineKeyboardMarkup(inline_keyboard=buttons[:10])

    @router.message(Command("signorders"))
    async def sign_orders_command(message: Message, command: CommandObject) -> None:
        user = db.get_or_create_user(message.chat.id, get_sender_data(message))
        arg = (command.args or "").strip().lower()
        role = "buyer" if arg in {"buy", "buyer", "bought", "мои", "купил", "заказал"} else "author"
        rows = db.list_sign_orders(message.chat.id, user.user_id, role=role, limit=10)
        if not rows:
            text = "Тебе пока не заказывали сигны." if role == "author" else "Ты пока не заказывал(а) сигны."
            await message.answer(text)
            return
        title = "✍️ <b>Сигны, которые заказали у тебя</b>" if role == "author" else "✍️ <b>Сигны, которые заказал(а) ты</b>"
        body = "\n\n".join(_format_sign_order(row, role=role) for row in rows)
        await message.answer(
            f"{title}\n\n{body}",
            reply_markup=_sign_order_keyboard(rows, viewer_id=user.user_id),
            parse_mode="HTML",
        )

    @router.message(Command("signstats"))
    async def sign_stats_command(message: Message) -> None:
        user = db.get_or_create_user(message.chat.id, get_sender_data(message))
        stats = db.sign_order_stats(message.chat.id, user.user_id)
        await message.answer(
            "📊 <b>Статистика сигн</b>\n\n"
            f"Тебе заказали: <b>{stats['authored_total']}</b>\n"
            f"Активных у тебя: <b>{stats['authored_active']}</b>\n"
            f"Оплаченных тобой сделанных: <b>{stats['authored_paid']}</b>\n"
            f"Заработано: <b>{stats['earned']}</b> 🍪\n\n"
            f"Ты заказал(а): <b>{stats['bought_total']}</b>\n"
            f"Твои активные заказы: <b>{stats['bought_active']}</b>\n"
            f"Потрачено: <b>{stats['spent']}</b> 🍪",
            parse_mode="HTML",
        )

    @router.message(Command("signreq", "signrequest"))
    async def sign_request_command(message: Message, command: CommandObject) -> None:
        if not message.reply_to_message or not message.reply_to_message.from_user:
            await message.answer("Ответь на сообщение автора сигны: <code>/signreq текст</code> или <code>/signreq номер_тарифа текст</code>", parse_mode="HTML")
            return
        raw_text = (command.args or "").strip()
        if not raw_text:
            await message.answer("Напиши текст заказа: <code>/signreq текст</code> или <code>/signreq номер_тарифа текст</code>", parse_mode="HTML")
            return
        buyer = db.get_or_create_user(message.chat.id, get_sender_data(message))
        target_sender = get_sender_data(message.reply_to_message)
        if target_sender.is_bot or target_sender.user_id == buyer.user_id:
            await message.answer("❌ Заказывать сигну можно только у другого человека.")
            return
        target = db.get_or_create_user(message.chat.id, target_sender)
        option_id = None
        option_title = None
        text = raw_text
        first_part, _, rest = raw_text.partition(" ")
        normalized_option = first_part.lstrip("#")
        price = target.sign_price or ai.settings.human_sign_min_price
        if normalized_option.isdigit() and rest.strip():
            option = db.get_sign_price_option(message.chat.id, target.user_id, int(normalized_option))
            if not option:
                await message.answer("❌ У автора нет такого активного тарифа. Посмотри цены: ответь ему <code>/signprice</code>", parse_mode="HTML")
                return
            option_id = int(option["id"])
            option_title = str(option.get("title") or "")
            price = int(option.get("price") or price)
            text = rest.strip()
        if buyer.reputation < price:
            await message.answer(f"❌ Нужно <b>{price}</b> 🍪. Баланс: <b>{buyer.reputation}</b> 🍪", parse_mode="HTML")
            return
        order = db.create_sign_order(buyer, target, price, text, option_id=option_id, option_title=option_title)
        if not order:
            await message.answer("❌ Не смог создать заказ. Проверь, применена ли миграция signs_and_ai_images.sql.")
            return
        order_id = int(order["id"])
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Принять", callback_data=f"sign_accept_{order_id}"),
                InlineKeyboardButton(text="Отказать", callback_data=f"sign_decline_{order_id}"),
            ]
        ])
        option_line = f"Тариф: <b>{escape_html(option_title)}</b>\n" if option_title else ""
        await message.answer(
            f"✍️ <b>Заказ сигны #{order_id}</b>\n\n"
            f"Автор: {escape_html(target.display_name)}\n"
            f"Покупатель: {escape_html(buyer.display_name)}\n"
            f"{option_line}"
            f"Цена: <b>{price}</b> 🍪\n"
            f"Текст: <i>{escape_html(text[:300])}</i>\n\n"
            "Автор должен принять заказ кнопкой. На сигне должен быть сам автор заказа: фото/рисунок/стиль на его выбор, "
            "но табличка с текстом должна быть видна. Деньги уйдут в эскроу и попадут автору после подтверждения.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("sign_"))
    async def sign_callback(query: CallbackQuery) -> None:
        parts = (query.data or "").split("_", 2)
        if len(parts) != 3 or not parts[2].isdigit():
            await query.answer("Заказ устарел.", show_alert=True)
            return
        action, order_id = parts[1], int(parts[2])
        order = db.get_sign_order(query.message.chat.id, order_id)
        if not order:
            await query.answer("Заказ не найден.", show_alert=True)
            return
        status = str(order.get("status"))
        author_id = int(order.get("author_id") or 0)
        buyer_id = int(order.get("buyer_id") or 0)
        price = int(order.get("price") or 0)
        now = datetime.now(timezone.utc).isoformat()

        if action in {"accept", "decline", "done", "cancel"} and query.from_user.id != author_id:
            await query.answer("Это может подтвердить только автор сигны.", show_alert=True)
            return
        if action == "pay" and query.from_user.id != buyer_id:
            await query.answer("Оплату подтверждает только покупатель.", show_alert=True)
            return

        if action == "decline":
            if status != "pending":
                await query.answer("Этот заказ уже нельзя отклонить.", show_alert=True)
                return
            db.update_sign_order(order_id, {"status": "cancelled", "cancelled_at": now, "cancel_reason": "declined_by_author"})
            await query.message.edit_text(f"✍️ Заказ сигны #{order_id} отклонён.")
            await query.answer()
            return

        if action == "accept":
            if status != "pending":
                await query.answer("Заказ уже обработан.", show_alert=True)
                return
            buyer = db.get_user_by_platform_id(int(order["chat_id"]), buyer_id)
            if not buyer or buyer.reputation < price:
                db.update_sign_order(order_id, {"status": "cancelled", "cancelled_at": now, "cancel_reason": "buyer_has_no_cookies"})
                await query.message.edit_text(f"✍️ Заказ #{order_id} отменён: у покупателя уже не хватает печенек.")
                await query.answer()
                return
            charged = db.update_user(buyer.id, {"reputation": buyer.reputation - price})
            if not charged:
                await query.answer("Не смог списать печеньки у покупателя.", show_alert=True)
                return
            updated = db.update_sign_order(order_id, {"status": "accepted", "escrow_amount": price, "accepted_at": now})
            if not updated:
                db.update_user(buyer.id, {"reputation": buyer.reputation})
                await query.answer("Не смог обновить заказ, списание откатил.", show_alert=True)
                return
            option_line = f"Тариф: <b>{escape_html(str(order.get('option_title')))}</b>\n" if order.get("option_title") else ""
            await query.message.edit_text(
                f"✍️ <b>Заказ #{order_id} принят</b>\n\n"
                f"Цена <b>{price}</b> 🍪 лежит в эскроу.\n"
                f"{option_line}"
                f"Текст: <i>{escape_html(str(order.get('text') or ''))}</i>\n\n"
                f"Когда сигна готова, автор жмет: <code>/signorders</code> → <b>Готово #{order_id}</b>.",
                parse_mode="HTML",
            )
            await query.answer()
            return

        if action == "done":
            if status != "accepted":
                await query.answer("Готовым можно отметить только заказ в работе.", show_alert=True)
                return
            db.update_sign_order(order_id, {"status": "delivered", "delivered_at": now})
            await query.message.edit_text(
                f"✍️ <b>Заказ #{order_id} отмечен готовым</b>\n\n"
                "Покупатель должен подтвердить получение через <code>/signorders мои</code>.",
                parse_mode="HTML",
            )
            await query.answer("Готово отмечено.")
            return

        if action == "cancel":
            if status not in {"pending", "accepted"}:
                await query.answer("Этот заказ уже нельзя отменить.", show_alert=True)
                return
            buyer = db.get_user_by_platform_id(int(order["chat_id"]), buyer_id)
            escrow = int(order.get("escrow_amount") or 0)
            if buyer and escrow > 0:
                db.update_user(buyer.id, {"reputation": buyer.reputation + escrow})
            db.update_sign_order(order_id, {"status": "cancelled", "escrow_amount": 0, "cancelled_at": now, "cancel_reason": "cancelled_by_author"})
            await query.message.edit_text(f"✍️ Заказ #{order_id} отменён. Эскроу возвращён покупателю.")
            await query.answer()
            return

        if action == "pay":
            if status != "delivered":
                await query.answer("Оплатить можно только готовый заказ.", show_alert=True)
                return
            author = db.get_user_by_platform_id(int(order["chat_id"]), author_id)
            escrow = int(order.get("escrow_amount") or price)
            if not author or escrow <= 0:
                await query.answer("Не смог найти автора или сумму оплаты.", show_alert=True)
                return
            paid_author = db.update_user(author.id, {"reputation": author.reputation + escrow})
            if not paid_author:
                await query.answer("Не смог начислить печеньки автору.", show_alert=True)
                return
            db.update_sign_order(order_id, {"status": "paid", "escrow_amount": 0, "paid_at": now})
            await query.message.edit_text(
                f"✍️ <b>Заказ #{order_id} закрыт</b>\n\n"
                f"{escape_html(author.display_name)} получил(а) <b>{escrow}</b> 🍪.",
                parse_mode="HTML",
            )
            await query.answer("Оплата подтверждена.")

    @router.message(Command("fish", "рыбалка"))
    async def fish_command(message: Message) -> None:
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await _deny_if_jailed(message, sender, "/fish"):
            return
        allowed, remaining = db.can_user_use_command(message.chat.id, sender.user_id, "fish", 45 * 60)
        if not allowed:
            await message.answer(f"🎣 Рыба пока не клюет. Возвращайся через {remaining // 60 + 1} мин.")
            return

        roll = random.random()
        if roll < 0.08:
            reward, xp, text = random.randint(2, 5), random.randint(3, 6), "🪣 Поймал старое ведро, но на дне прилипли печеньки."
        elif roll < 0.55:
            reward, xp, text = random.randint(8, 18), random.randint(6, 10), "🐟 Обычная рыбка."
        elif roll < 0.84:
            reward, xp, text = random.randint(22, 38), random.randint(9, 15), "🐠 Редкий улов."
        elif roll < 0.975:
            reward, xp, text = random.randint(45, 80), random.randint(14, 22), "🦑 Очень жирный улов."
        else:
            reward, xp, text = random.randint(120, 190), random.randint(24, 40), "🧰 Сундук со дна!"

        updated_sender = db.add_reputation(sender, reward) or sender
        db.add_xp(updated_sender, xp)
        await message.answer(
            f"🎣 <b>Рыбалка</b>\n\n"
            f"{text}\n"
            f"Награда: <b>{reward}</b> 🍪 и <b>{xp}</b> XP\n"
            f"Баланс: <b>{sender.reputation + reward}</b> 🍪",
            parse_mode="HTML",
        )

    @router.message(Command("drop", "cookie_drop"))
    async def drop_command(message: Message) -> None:
        if not _is_group_chat(message):
            await message.answer("🍪 Печеньки в чат падают только в группах.")
            return
        allowed, remaining = db.can_use_command(message.chat.id, "cookie_drop", 35 * 60)
        if not allowed:
            await message.answer(f"🍪 Следующая печенька упадет примерно через {remaining // 60 + 1} мин.")
            return
        reward = random.randint(10, 28)
        created_at = int(time.time())
        msg = await message.answer(
            "🍪 <b>Печенька упала в чат!</b>\nКто первый нажмет, тот заберет награду.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🍪 Забрать {reward}", callback_data=f"drop_claim_{reward}_{created_at}")]
            ]),
        )
        _drop_sessions[f"{message.chat.id}_{msg.message_id}"] = {
            "reward": reward,
            "created_at": created_at,
        }

    @router.callback_query(F.data.startswith("drop_claim"))
    async def drop_callback(query: CallbackQuery) -> None:
        if not query.message:
            await query.answer("Печенька уже исчезла.", show_alert=True)
            return
        key = f"{query.message.chat.id}_{query.message.message_id}"
        event_key = f"drop_{key}"
        if event_key in AUTO_CLAIMED_EVENTS:
            await query.answer("Печеньку уже забрали.", show_alert=True)
            return
        session = _drop_sessions.get(key)
        reward = None
        created_at = None
        if session:
            reward = int(session["reward"])
            created_at = int(session["created_at"])
        elif query.data:
            parts = query.data.split("_")
            if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
                reward = int(parts[2])
                created_at = int(parts[3])

        if reward is None or created_at is None or time.time() - created_at > 10 * 60:
            _drop_sessions.pop(key, None)
            await query.answer("Печенька уже исчезла.", show_alert=True)
            return

        user = db.get_or_create_user(query.message.chat.id, _callback_sender(query))
        if _jail_remaining(user):
            await query.answer("Из тюрьмы печеньки не ловятся.", show_alert=True)
            return
        AUTO_CLAIMED_EVENTS.add(event_key)
        _drop_sessions.pop(key, None)
        db.update_user(user.id, {"reputation": user.reputation + reward})
        await query.message.edit_text(
            f"🍪 <b>{escape_html(user.display_name)} забрал печеньку!</b>\n"
            f"Награда: <b>{reward}</b> 🍪",
            parse_mode="HTML",
        )
        await query.answer(f"+{reward} 🍪")

    @router.message(Command("quiz", "викторина"))
    async def quiz_command(message: Message) -> None:
        if not _is_group_chat(message):
            await message.answer("🧠 Викторина запускается только в группах.")
            return
        allowed, remaining = db.can_use_command(message.chat.id, "quiz", 25 * 60)
        if not allowed:
            await message.answer(f"🧠 Следующий вопрос через {remaining // 60 + 1} мин.")
            return
        quiz = make_quiz_question()
        created_at = int(time.time())
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=str(quiz["options"][0]), callback_data=f"quiz_answer_0_{quiz['answer_idx']}_{quiz['reward']}_{created_at}_{quiz['options'][quiz['answer_idx']]}"),
                InlineKeyboardButton(text=str(quiz["options"][1]), callback_data=f"quiz_answer_1_{quiz['answer_idx']}_{quiz['reward']}_{created_at}_{quiz['options'][quiz['answer_idx']]}"),
            ],
            [
                InlineKeyboardButton(text=str(quiz["options"][2]), callback_data=f"quiz_answer_2_{quiz['answer_idx']}_{quiz['reward']}_{created_at}_{quiz['options'][quiz['answer_idx']]}"),
                InlineKeyboardButton(text=str(quiz["options"][3]), callback_data=f"quiz_answer_3_{quiz['answer_idx']}_{quiz['reward']}_{created_at}_{quiz['options'][quiz['answer_idx']]}"),
            ],
        ])
        msg = await message.answer(
            f"🧠 <b>Викторина</b>\n\n"
            f"Сколько будет: <code>{quiz['question']}</code>?\n"
            f"Награда: <b>{quiz['reward']}</b> 🍪",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        _quiz_sessions[f"{message.chat.id}_{msg.message_id}"] = {
            **quiz,
            "created_at": created_at,
            "wrong_users": set(),
        }

    @router.callback_query(F.data.startswith("quiz_answer_"))
    async def quiz_callback(query: CallbackQuery) -> None:
        if not query.message or not query.data:
            await query.answer("Вопрос уже исчез.", show_alert=True)
            return
        try:
            parts = query.data.split("_")
            answer_idx = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("Кнопка устарела.", show_alert=True)
            return

        key = f"{query.message.chat.id}_{query.message.message_id}"
        event_key = f"quiz_{key}"
        if event_key in AUTO_CLAIMED_EVENTS:
            await query.answer("Приз уже забрали.", show_alert=True)
            return
        session = _quiz_sessions.get(key)
        answer_value = None
        if (
            not session
            and len(parts) == 7
            and parts[3].isdigit()
            and parts[4].isdigit()
            and parts[5].isdigit()
            and parts[6].isdigit()
        ):
            session = {
                "answer_idx": int(parts[3]),
                "reward": int(parts[4]),
                "created_at": int(parts[5]),
                "options": [None, None, None, None],
                "wrong_users": set(),
            }
            answer_value = parts[6]
        if session:
            answer_value = answer_value or str(session["options"][session["answer_idx"]])

        if not session or time.time() - int(session["created_at"]) > 10 * 60:
            _quiz_sessions.pop(key, None)
            await query.answer("Вопрос уже устарел.", show_alert=True)
            return
        if query.from_user.id in session["wrong_users"]:
            await query.answer("У тебя уже была попытка.", show_alert=True)
            return
        if answer_idx != session["answer_idx"]:
            session["wrong_users"].add(query.from_user.id)
            await query.answer("Мимо. Одна попытка на человека.", show_alert=True)
            return

        user = db.get_or_create_user(query.message.chat.id, _callback_sender(query))
        if _jail_remaining(user):
            await query.answer("Из тюрьмы в викторине не участвуют.", show_alert=True)
            return
        AUTO_CLAIMED_EVENTS.add(event_key)
        _quiz_sessions.pop(key, None)
        reward = int(session["reward"])
        updated_user = db.add_reputation(user, reward) or user
        db.add_xp(updated_user, 8)
        await query.message.edit_text(
            f"🧠 <b>Правильный ответ: {answer_value}</b>\n"
            f"Победитель: <b>{escape_html(user.display_name)}</b>\n"
            f"Награда: <b>{reward}</b> 🍪 и <b>8</b> XP",
            parse_mode="HTML",
        )
        await query.answer(f"+{reward} 🍪")

    _casino_double_sessions = {}

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
        if await _deny_if_jailed(message, sender, "/casino"):
            return
        if await _deny_bad_bet(message, sender, bet):
            return
        
        if sender.reputation < bet:
            await message.answer(f"❌ Недостаточно печенек! У тебя всего: <b>{sender.reputation}</b> 🍪")
            return

        allowed, remaining = db.can_use_command(message.chat.id, f"casino_{sender.user_id}", 20)
        if not allowed:
            await message.answer(
                f"⏳ <b>Автомат остывает.</b>\nПодожди ещё <code>{remaining} сек.</code>",
                parse_mode="HTML"
            )
            return

        # Налог в джекпот
        chat_settings = db.get_chat_settings(message.chat.id)
        current_jackpot = chat_settings.casino_jackpot
        tax = max(1, bet // 100)
        db.update_user(sender.id, {"reputation": sender.reputation - bet})
        db.update_chat_settings(message.chat.id, casino_jackpot=current_jackpot + tax)
        current_jackpot += tax

        # Символы слотов
        symbols = ["🍒", "🍋", "🍇", "🍉", "🔔", "💎", "🎰"]
        r1, r2, r3 = random.choice(symbols), random.choice(symbols), random.choice(symbols)
        symbol_rules = {
            # pair — множитель возврата (>1.0 = прибыль, <1.0 = частичный возврат)
            "🍒": {"name": "Вишневый ряд",        "triple": 5.0,  "pair": 1.10},
            "🍋": {"name": "Лимонная линия",       "triple": 4.5,  "pair": 1.05},
            "🍇": {"name": "Виноградный сбор",     "triple": 6.0,  "pair": 1.15},
            "🍉": {"name": "Арбузный куш",         "triple": 7.0,  "pair": 1.25},
            "🔔": {"name": "Колокольный звон",     "triple": 10.0, "pair": 1.50},
            "💎": {"name": "Бриллиантовая линия",  "triple": 16.0, "pair": 1.80},
            "🎰": {"name": "Легендарный слот",     "triple": 35.0, "pair": 2.20},
        }
        fruit_symbols = {"🍒", "🍋", "🍇", "🍉"}
        premium_symbols = {"🔔", "💎", "🎰"}
        
        # Анимация "Саспенс"
        msg = await message.answer(f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\n💰 Ставка: <code>{bet}</code> 🍪\n🏆 Джекпот: <code>{current_jackpot}</code>\n\n[ 🎲 | 🎲 | 🎲 ]", parse_mode="HTML")
        await asyncio.sleep(0.5)
        
        await msg.edit_text(f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\n💰 Ставка: <code>{bet}</code> 🍪\n🏆 Джекпот: <code>{current_jackpot}</code>\n\n[ {r1} | 🎲 | 🎲 ]", parse_mode="HTML")
        await asyncio.sleep(0.5)
        
        await msg.edit_text(f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\n💰 Ставка: <code>{bet}</code> 🍪\n🏆 Джекпот: <code>{current_jackpot}</code>\n\n[ {r1} | {r2} | 🎲 ]", parse_mode="HTML")
        
        # Если первые два совпали, делаем паузу длиннее - интрига!
        if r1 == r2:
            await asyncio.sleep(1.2)
        else:
            await asyncio.sleep(0.5)

        final_symbols = f" [ {r1} | {r2} | {r3} ] "
        result_text = ""
        win_total = 0
        is_jackpot = False
        consol_xp = 0

        reels = [r1, r2, r3]
        counts = {symbol: reels.count(symbol) for symbol in symbols}
        pair_symbol = next((symbol for symbol, count in counts.items() if count == 2), None)

        if r1 == r2 == r3:
            if r1 == "🎰":
                is_jackpot = True
                jackpot_bonus = min(current_jackpot, bet * 8)
                win_total = int(bet * symbol_rules[r1]["triple"]) + jackpot_bonus
                db.update_chat_settings(message.chat.id, casino_jackpot=500)
                result_text = (
                    f"🌌 <b>ЛЕГЕНДАРНЫЙ ДЖЕКПОТ!!!</b>\n\n"
                    f"Выигрыш x35 и джекпот-бонус <b>{jackpot_bonus}</b> 🍪!\n"
                    f"Итого: <b>{win_total}</b> 🍪 🎉🍾"
                )
            else:
                multiplier = symbol_rules[r1]["triple"]
                win_total = int(bet * multiplier)
                result_text = f"✨ <b>{symbol_rules[r1]['name'].upper()}!</b> Три в ряд x{multiplier:g}!"
        elif pair_symbol:
            multiplier = symbol_rules[pair_symbol]["pair"]
            win_total = max(1, int(bet * multiplier))
            result_text = f"🥂 <b>ПАРА: {pair_symbol}{pair_symbol}</b> {symbol_rules[pair_symbol]['name']} x{multiplier:g}"
        elif set(reels).issubset(fruit_symbols):
            # Три разных фрукта — маленький утешительный возврат
            win_total = max(1, int(bet * 0.70))
            result_text = "🍹 <b>ФРУКТОВЫЙ МИКС!</b> Три разных фрукта — возврат x0.70"
        elif set(reels).issubset(premium_symbols):
            # Три разных дорогих — хорошая комбинация
            win_total = int(bet * 1.50)
            result_text = "⚡ <b>ПРЕМИУМ-ЛИНИЯ!</b> Три разных дорогих символа x1.50"
        elif sum(1 for symbol in reels if symbol in premium_symbols) >= 2:
            # Два дорогих + один дешевый — частичный возврат
            win_total = max(1, int(bet * 0.60))
            result_text = "💠 <b>ПОЧТИ ПРЕМИУМ!</b> Два дорогих символа — возврат x0.60"
        elif sum(1 for symbol in reels if symbol in fruit_symbols) >= 2:
            # Два фруктовых + один дорогой — крошки
            win_total = max(1, int(bet * 0.35))
            result_text = "🍬 <b>СЛАДКИЙ МИКС!</b> Два фруктовых — крошки x0.35"
        else:
            win_total = 0
            consol_xp = random.randint(2, 5)
            result_text = (
                f"💨 <b>НЕУДАЧА!</b> В следующий раз повезет!\n<i>(Утешительный приз: +{consol_xp} XP)</i>"
            )

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
        if win_total > 0 and not is_jackpot:
            _casino_double_sessions[f"{message.chat.id}_{msg.message_id}"] = {
                "user_id": sender.user_id,
                "win_amount": win_total,
            }

    @router.callback_query(F.data.startswith("dice_double_"))
    async def casino_double_callback(query: CallbackQuery) -> None:
        try:
            parts = query.data.split("_")
            user_id = int(parts[2])
            win_amount = int(parts[3])
        except (IndexError, TypeError, ValueError):
            await query.answer("❌ Некорректная кнопка.", show_alert=True)
            return
        
        if query.from_user.id != user_id:
            await query.answer("❌ Это не твой выигрыш!", show_alert=True)
            return

        lock_key = f"{query.message.chat.id}_{query.message.message_id}"
        session = _casino_double_sessions.get(lock_key)
        if not session:
            await query.answer("⏳ Этот риск уже недоступен.", show_alert=True)
            return
        if session["user_id"] != user_id or session["win_amount"] != win_amount:
            await query.answer("❌ Кнопка не совпадает с этим выигрышем.", show_alert=True)
            return

        sender = db.get_user_by_platform_id(query.message.chat.id, user_id)
        if not sender: return

        if sender.reputation < win_amount:
            await query.answer("❌ На балансе уже не хватает этого выигрыша.", show_alert=True)
            return

        _casino_double_sessions.pop(lock_key, None)
        
        # Удвоение: при победе получаешь +win_amount сверху (итого x2 выигрыш),
        # при проигрыше теряешь win_amount (уже зачисленный при спине).
        # Шанс 45% делает EV = 0.45*2 + 0.55*0 = 0.90 от win — небольшой минус, честно.
        if random.random() < 0.45:
            bonus = win_amount  # дополнительный выигрыш сверху уже имеющегося
            new_balance = sender.reputation + bonus
            db.update_user(sender.id, {"reputation": new_balance})
            await query.message.edit_text(
                f"🃏 <b>РИСК ОПРАВДАН!</b>\n\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                f"🔥 Ты удвоил куш: +<b>{bonus}</b> 🍪 бонуса!\n"
                f"📈 Баланс: <b>{new_balance}</b> 🍪",
                parse_mode="HTML"
            )
            await query.answer("💰 Удвоено!")
        else:
            # Проигрыш — теряешь весь win_amount (он уже был зачислен на баланс)
            new_balance = sender.reputation - win_amount
            db.update_user(sender.id, {"reputation": max(0, new_balance)})
            await query.message.edit_text(
                f"🃏 <b>РИСК НЕ ОПРАВДАН...</b>\n\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                f"💀 Потерял выигрыш: <b>{win_amount}</b> 🍪\n"
                f"📉 Баланс: <b>{max(0, new_balance)}</b> 🍪",
                parse_mode="HTML"
            )
            await query.answer("💀 Потрачено", show_alert=True)

    def _get_rank_name(level: int) -> str:
        if level >= 50: return "Легенда"
        if level >= 30: return "Элита"
        if level >= 20: return "Мастер"
        if level >= 10: return "Опытный"
        if level >= 5: return "Активный"
        return "Новичок"

    def _get_tower_multiplier(floor: int) -> float:
        multipliers = [0.90, 1.00, 1.14, 1.42, 2.00, 2.90, 4.60, 8.10, 16.0, 40.0]
        if floor < 1: return 1.0
        if floor > 10: return multipliers[-1]
        return multipliers[floor-1]

    def _get_tower_reward_multiplier(floor: int, weather: dict) -> float:
        weather_mod = weather["reward_mod"] if floor > 1 else 1.0
        return _get_tower_multiplier(floor) * weather_mod

    def _get_tower_success_chance(floor: int, weather: dict) -> float:
        chances = [0.95, 0.86, 0.77, 0.67, 0.57, 0.48, 0.39, 0.31, 0.23, 0.16]
        base_chance = chances[floor - 1] if 1 <= floor <= len(chances) else 0.20
        return min(0.97, max(0.08, base_chance + weather["chance_mod"]))

    def _format_tower_chance(chance: float) -> str:
        percent = round(chance * 100, 1)
        return f"{percent:.0f}%" if percent.is_integer() else f"{percent:.1f}%"

    # --- ИГРА: БАШНЯ ФОРТУНЫ (COMPACT & DYNAMIC VERSION) ---
    _tower_locks = {}
    _tower_sessions = {}

    @router.message(Command("tower", "башня", "climb"))
    async def tower_command(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer(
                "🏰 <b>Башня Фортуны</b>\n\n"
                "🛡 <b>Страховка:</b> уже с 3 этажа, сильнее на 5 и 8\n"
                "Использование: <code>/tower <ставка></code>", 
                parse_mode="HTML"
            )
            return
            
        bet = int(command.args.strip())
        sender_data = get_sender_data(message)
        sender = db.get_or_create_user(message.chat.id, sender_data)
        if await _deny_if_jailed(message, sender, "/tower"):
            return
        
        if await _deny_bad_bet(message, sender, bet, min_bet=5):
            return
            
        if sender.reputation < bet:
            await message.answer(f"❌ Недостаточно печенек! У тебя: {sender.reputation} 🍪")
            return

        allowed, remaining = db.can_use_command(message.chat.id, f"tower_{sender.user_id}", 30)
        if not allowed:
            await message.answer(f"⏳ Башня на отдыхе. Подожди {remaining} сек.", parse_mode="HTML")
            return

        weathers = [
            {"name": "☀️ Ясно", "chance_mod": 0.02, "reward_mod": 1.0},
            {"name": "🌫 Туман", "chance_mod": -0.03, "reward_mod": 1.06},
            {"name": "🌪 Буря", "chance_mod": -0.07, "reward_mod": 1.14},
            {"name": "🌈 Попутный ветер", "chance_mod": 0.05, "reward_mod": 0.92}
        ]
        weather = random.choice(weathers)
        
        db.update_user(sender.id, {"reputation": sender.reputation - bet})
        sent_msg = await _show_tower(message, floor=1, bet=bet, user_id=sender.user_id, is_new=True, weather=weather, event_msg="Ты у подножия башни.")
        if sent_msg:
            _tower_sessions[f"{message.chat.id}_{sent_msg.message_id}"] = {
                "user_id": sender.user_id,
                "floor": 1,
                "bet": bet,
                "weather_idx": weathers.index(weather),
            }

    @router.callback_query(F.data.startswith("tower_"))
    async def tower_callback(query: CallbackQuery) -> None:
        try:
            parts = query.data.split("_")
            action = parts[1]
            floor = int(parts[2])
            bet = int(parts[3])
            original_user_id = int(parts[4])
            weather_idx = int(parts[5]) if len(parts) > 5 else 0
        except (IndexError, TypeError, ValueError):
            await query.answer("❌ Некорректная кнопка.", show_alert=True)
            return
        
        weathers = [
            {"name": "☀️ Ясно", "chance_mod": 0.02, "reward_mod": 1.0},
            {"name": "🌫 Туман", "chance_mod": -0.03, "reward_mod": 1.06},
            {"name": "🌪 Буря", "chance_mod": -0.07, "reward_mod": 1.14},
            {"name": "🌈 Попутный ветер", "chance_mod": 0.05, "reward_mod": 0.92}
        ]
        if action not in {"up", "take"} or floor < 1 or bet < 1 or weather_idx not in range(len(weathers)):
            await query.answer("❌ Некорректная кнопка.", show_alert=True)
            return
        weather = weathers[weather_idx]

        if query.from_user.id != original_user_id:
            await query.answer("❌ Не твой подъем!", show_alert=True)
            return
            
        now = time.time()
        lock_key = f"{query.message.chat.id}_{query.message.message_id}"
        session = _tower_sessions.get(lock_key)
        if not session:
            await query.answer("⏳ Эта башня уже завершена или устарела.", show_alert=True)
            return
        if (
            session["user_id"] != original_user_id
            or session["floor"] != floor
            or session["bet"] != bet
            or session["weather_idx"] != weather_idx
        ):
            await query.answer("❌ Этот ход уже неактивен.", show_alert=True)
            return

        if lock_key in _tower_locks and now - _tower_locks[lock_key] < 0.8:
            await query.answer("⏳ Подожди секунду...", show_alert=False)
            return
        _tower_locks[lock_key] = now

        sender = db.get_user_by_platform_id(query.message.chat.id, original_user_id)
        if not sender: return

        if action == "take":
            _tower_sessions.pop(lock_key, None)
            multiplier = _get_tower_reward_multiplier(floor, weather)
            win = int(bet * multiplier)
            db.update_user(sender.id, {"reputation": sender.reputation + win})
            
            await query.message.edit_text(
                f"🎉 <b>ТЫ ЗАБРАЛ ПРИЗ!</b>\n\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                f"🪜 Достигнут этаж: <b>{floor}</b>\n"
                f"💰 Выигрыш: <b>{win} 🍪</b>\n\n"
                f"📈 Баланс: <b>{sender.reputation + win}</b> 🍪",
                parse_mode="HTML"
            )
            await query.answer("💰 Приз в кармане!")
            
        elif action == "up":
            success_chance = _get_tower_success_chance(floor, weather)
            
            # Случайные события (Random Encounters)
            event_msg = ""
            if random.random() < 0.10: # 10% шанс на особое событие
                event_amount = max(1, int(bet * 0.08))
                events = [
                    (f"🎁 Нашел старую заначку! (+{event_amount} 🍪)", lambda u: db.add_reputation(u, event_amount)),
                    (f"🍪 Карманный бонус! (+{event_amount} 🍪)", lambda u: db.add_reputation(u, event_amount)),
                    (f"🪤 Наступил на ловушку! (-{event_amount} 🍪)", lambda u: db.add_reputation(u, -event_amount)),
                    ("🔮 Загадочная аура: башня на секунду стала тише.", lambda u: None)
                ]
                ev_text, ev_action = random.choice(events)
                event_msg = ev_text
                ev_action(sender)
                sender = db.get_user_by_platform_id(query.message.chat.id, original_user_id) or sender

            if random.random() < success_chance:
                new_floor = floor + 1
                if new_floor > 10:
                    multiplier = _get_tower_reward_multiplier(10, weather)
                    win = int(bet * multiplier)
                    _tower_sessions.pop(lock_key, None)
                    db.update_user(sender.id, {"reputation": sender.reputation + win})
                    await query.message.edit_text(f"🏆 <b>ТЫ ПОКОРИЛ ВЕРШИНУ БАШНИ!</b> (x{multiplier:.1f})\n💰 Твой куш: {win} 🍪", parse_mode="HTML")
                    await query.answer("🏆 Вершина взята!")
                    return

                try:
                    await _show_tower(query.message, floor=new_floor, bet=bet, user_id=original_user_id, is_new=False, weather=weather, event_msg=event_msg)
                    _tower_sessions[lock_key]["floor"] = new_floor
                    await query.answer("✅ Успешный подъем!")
                except Exception as exc:
                    print(f"[tower:error] edit failed: {exc}")
                    await query.answer("❌ Не удалось обновить башню. Попробуй ещё раз.", show_alert=True)
            else:
                # Чекпоинты
                safe_win = 0
                mult = _get_tower_reward_multiplier(floor, weather)
                # Страховка растет вместе с прогрессом:
                # 1 эт: 30% ставки, 2 эт: 35%, 3-4 эт: 40%, 5-7 эт: 50% куша, 8+ эт: 70% куша
                if floor <= 1:   safe_win = int(bet * 0.30)
                elif floor == 2: safe_win = int(bet * 0.35)
                elif floor <= 4: safe_win = int(bet * 0.40)
                elif floor <= 7: safe_win = int(bet * mult * 0.50)
                else:            safe_win = int(bet * mult * 0.70)

                if safe_win > 0:
                    db.update_user(sender.id, {"reputation": sender.reputation + safe_win})

                _tower_sessions.pop(lock_key, None)
                await query.message.edit_text(
                    f"💀 <b>ТЫ СОРВАЛСЯ ВНИЗ!</b>\n\n"
                    f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                    f"🪜 Упал на <b>{floor + 1}</b> этаже.\n"
                    f"🛡 Сработала страховка: <b>{safe_win}</b> 🍪\n\n"
                    f"📈 Баланс: <b>{sender.reputation + safe_win}</b> 🍪",
                    parse_mode="HTML"
                )
                await query.answer("💥 БА-БАХ!", show_alert=True)

    async def _show_tower(msg: Message, floor: int, bet: int, user_id: int, is_new: bool, weather: dict, event_msg: str = "") -> Message | None:
        multiplier = _get_tower_reward_multiplier(floor, weather)
        weathers_list = ["☀️ Ясно", "🌫 Туман", "🌪 Буря", "🌈 Попутный ветер"]
        w_idx = weathers_list.index(weather["name"]) if weather["name"] in weathers_list else 0

        # Компактный Радар (показываем только 3 этажа)
        tower_lines = []
        for i in range(min(10, floor + 1), max(0, floor - 2), -1):
            m = _get_tower_reward_multiplier(i, weather)
            m_text = f"x{m:.1f}"
            
            if i == floor:
                line = f"▶️ <b>[{i:02}] {m_text} 🏃 ТЫ ТУТ</b>"
            elif i < floor:
                line = f"✅ <code>[{i:02}] {m_text}</code>"
            else:
                prefix = "🪜" if i not in (5, 8, 10) else ("🥉" if i==5 else "🥈" if i==8 else "💎")
                line = f"{prefix} <code>[{i:02}] {m_text}</code>"
            tower_lines.append(line)

        progress = "▰" * floor + "▱" * (10 - floor)
        event_block = f"\n<i>{event_msg}</i>\n" if event_msg else ""
        success_chance = _get_tower_success_chance(floor, weather)
        fail_chance = 1.0 - success_chance
        next_floor = min(10, floor + 1)
        next_multiplier = _get_tower_reward_multiplier(next_floor, weather)
        next_prize = int(bet * next_multiplier)
        current_prize = int(bet * multiplier)
        if floor >= 10:
            next_result_text = f"🏆 При успехе: <b>вершина</b>, куш <b>{next_prize} 🍪</b>\n"
        else:
            next_result_text = f"🪜 При успехе: <b>{next_floor}/10</b>, куш <b>{next_prize} 🍪</b>\n"
        
        text = (
            f"🏰 <b>БАШНЯ ФОРТУНЫ</b>\n"
            f"🌤 Погода: <b>{weather['name']}</b>\n"
            f"<code>{progress}</code> {floor}/10\n"
            f"{event_block}\n"
            + "\n".join(tower_lines) + "\n\n" +
            f"💰 Ставка: <code>{bet}</code> 🍪 | 💵 Текущий куш: <b>{current_prize} 🍪</b>\n"
            f"🎯 Шанс кнопки: ✅ <b>{_format_tower_chance(success_chance)}</b> пройти / 💥 <b>{_format_tower_chance(fail_chance)}</b> сорваться\n"
            f"{next_result_text}"
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⏫ ЛЕЗТЬ ДАЛЬШЕ", callback_data=f"tower_up_{floor}_{bet}_{user_id}_{w_idx}"),
                InlineKeyboardButton(text="💰 ЗАБРАТЬ", callback_data=f"tower_take_{floor}_{bet}_{user_id}_{w_idx}")
            ]
        ])
        
        if is_new:
            return await msg.answer(text, reply_markup=kb, parse_mode="HTML")
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        return msg

    return router
