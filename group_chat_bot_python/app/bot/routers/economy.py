import random
import time
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
)

def build_economy_router(db: SupabaseDB, ai: AIService) -> Router:
    router = Router(name="economy")
    
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

    
    return router
