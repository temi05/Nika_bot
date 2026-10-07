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
from aiogram.exceptions import TelegramBadRequest

from app.services.ai_service import AIService
from app.services.supabase_db import SupabaseDB
from app.bot.admin import is_admin

from app.bot.routers.jail_helpers import (
    bail_cost,
    deny_if_jailed,
    format_jail_remaining,
    jail_remaining,
    jail_user,
    loan_limit,
    parse_iso_dt,
    pay_bail,
)
from app.models import MemoryRecord, Sender
from app.utils import (
    build_progress_bar,
    escape_html,
    get_sender_data,
    human_timedelta,
    parse_birthday_parts,
)

from app.bot.routers import game_sessions

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


def build_general_admin_router(db: SupabaseDB, bot_name: str, ai: AIService) -> Router:
    router = Router(name="general_admin")

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
            "🎭 <b>Интерактив и Творчество</b>\n\n"
            "• <code>/aiimage [описание]</code> — ИИ-картинка за печеньки\n"
            "• <code>/signai [текст]</code> — ИИ-сигна за печеньки\n"
            "• <code>/setsignprice</code>, <code>/signreq</code>, <code>/signorders</code> — Сигны от людей\n"
            "• Авто-события: выпадение печенек и викторины в активных чатах\n"
            "• <code>/rp [действие]</code> — Ролевые взаимодействия с участниками\n"
            "• Бросить монетку или спросить совет можно прямо в диалоге с ИИ Ники!"
        ),
        "profile": (
            "👤 <b>Персонализация и Память</b>\n\n"
            "• <code>/card</code> — Персональная карточка участника (отношения, зодиак, память)\n"
            "• <code>/bio [текст]</code> — Расскажи о себе\n"
            "• <code>/mybirthday [дата]</code> — Установи день рождения (DD.MM)\n"
            "• <code>/remember [факт]</code> — Сохранить важный факт в память Ники\n"
            "• <code>/lore</code> — Легенды и Лента Событий чата\n"
            "• <code>/add_lore [текст]</code> — Зафиксировать новую легенду чата\n"
            "• <code>/forget_me</code> — Стереть воспоминания о себе (чистый лист)\n"
            "• <code>/notes</code> — Заметки ИИ про участника"
        ),
        "admin": (
            "🛡 <b>Администрирование и Память</b>\n\n"
            "• <code>/clean_memory</code> — Очистить базу от бытового мусора\n"
            "• <code>/backup_memory</code> — Ручной бэкап векторной памяти в Telegram\n"
            "• <code>/restore_memory</code> — Восстановить базу из zip-архива\n"
            "• <code>/nika_brain [запрос]</code> — Поиск векторов в мозге Ники\n"
            "• <code>/all_facts</code> — Меню всех сохранённых фактов\n"
            "• <code>/export_facts</code> — Скачать всю базу знаний файлом .txt\n"
            "• <code>/cookie_rain</code> — Массовая раздача бонусов\n"
            "• <code>/whisper [текст]</code> — Сообщение от имени бота\n"
            "• <code>/judge</code> — Посадить или выпустить игрока\n"
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
                InlineKeyboardButton(text="🎭 Интерактив", callback_data="help_games")
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
            try:
                await query.message.edit_text(
                    HELP_PAGES[page],
                    reply_markup=get_help_keyboard(),
                    parse_mode="HTML"
                )
            except TelegramBadRequest:
                # Игнорируем ошибку, если сообщение не изменилось
                pass
            except Exception as e:
                # Другие ошибки логируем или игнорируем
                print(f"[help_callback:error] {e}")
        await query.answer()

    @router.callback_query(F.data.startswith("loan_"))
    async def loan_callback(query: CallbackQuery) -> None:
        parts = query.data.split("_")
        action = parts[1]
        session_key = f"{query.message.chat.id}_{query.message.message_id}"
        session = game_sessions.LOAN_SESSIONS.get(session_key)
        if not session:
            await query.answer("⏳ Это предложение уже неактивно.", show_alert=True)
            return
        
        if action == "dec":
            target_id = int(parts[2])
            if query.from_user.id != target_id:
                await query.answer("Это не вам предложили!", show_alert=True)
                return
            game_sessions.LOAN_SESSIONS.pop(session_key, None)
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
        if target.debt + amount > loan_limit(target):
            await query.message.edit_text("❌ Сделка отменена: у получателя уже слишком большой долг.")
            return
             
        # Выполняем сделку
        game_sessions.LOAN_SESSIONS.pop(session_key, None)
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

    @router.callback_query(F.data.startswith("aloan_"))
    async def ask_loan_callback(query: CallbackQuery) -> None:
        parts = query.data.split("_")
        action = parts[1]
        session_key = f"{query.message.chat.id}_{query.message.message_id}"
        session = game_sessions.LOAN_SESSIONS.get(session_key)
        if not session:
            await query.answer("⏳ Этот запрос уже неактивен.", show_alert=True)
            return
        
        if action == "no":
            original_asker_id = int(parts[2])
            if query.from_user.id != session.get("lender_id"):
                await query.answer("Отказать должен тот, у кого просили.", show_alert=True)
                return
            game_sessions.LOAN_SESSIONS.pop(session_key, None)
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
        if asker.debt + amount > loan_limit(asker):
            await query.message.edit_text("❌ Сделка отменена: у просителя уже слишком большой долг.")
            return
             
        # Выполняем сделку
        game_sessions.LOAN_SESSIONS.pop(session_key, None)
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
        session = game_sessions.BAIL_SESSIONS.get(session_key)
        if not session or time.time() - session["created_at"] > 300:
            game_sessions.BAIL_SESSIONS.pop(session_key, None)
            await query.answer("Залог устарел. Напиши /bail еще раз.", show_alert=True)
            return

        if action == "cancel":
            game_sessions.BAIL_SESSIONS.pop(session_key, None)
            await query.message.edit_text("❌ Залог отменен.")
            await query.answer()
            return

        user = db.get_user_by_platform_id(query.message.chat.id, user_id)
        if not user:
            game_sessions.BAIL_SESSIONS.pop(session_key, None)
            await query.answer("Профиль не найден.", show_alert=True)
            return
        remaining = jail_remaining(db, user)
        if not remaining:
            game_sessions.BAIL_SESSIONS.pop(session_key, None)
            await query.message.edit_text("✅ Ты уже на свободе.")
            await query.answer()
            return
        cost = int(session["cost"])
        if user.reputation < cost:
            await query.answer(f"Не хватает печенек: нужно {cost}, у тебя {user.reputation}.", show_alert=True)
            return

        result = pay_bail(db, user, cost)
        game_sessions.BAIL_SESSIONS.pop(session_key, None)
        await query.message.edit_text(
            f"🔓 <b>Залог оплачен.</b>\n"
            f"Стоимость: <b>{cost} 🍪</b>\n"
            f"Кредиторам ушло: <b>{result['creditor_part']} 🍪</b>\n"
            f"Ты снова на свободе.",
            parse_mode="HTML",
        )
        await query.answer()

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
        
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(command.args)

    _drop_sessions = game_sessions.AUTO_DROP_SESSIONS
    _quiz_sessions = game_sessions.AUTO_QUIZ_SESSIONS

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
        if event_key in game_sessions.AUTO_CLAIMED_EVENTS:
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
        if jail_remaining(db, user):
            await query.answer("Из тюрьмы печеньки не ловятся.", show_alert=True)
            return
        game_sessions.AUTO_CLAIMED_EVENTS.add(event_key)
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
        if event_key in game_sessions.AUTO_CLAIMED_EVENTS:
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
        if jail_remaining(db, user):
            await query.answer("Из тюрьмы в викторине не участвуют.", show_alert=True)
            return
        game_sessions.AUTO_CLAIMED_EVENTS.add(event_key)
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

    return router



