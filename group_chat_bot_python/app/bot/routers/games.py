import asyncio
import random
import time
from datetime import datetime, timezone, timedelta

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.services.ai_service import AIService
from app.services.supabase_db import SupabaseDB
from app.bot.routers.jail_helpers import (
    deny_if_jailed,
    format_jail_remaining,
    jail_remaining,
    jail_user,
    parse_iso_dt,
)
from app.bot.routers import game_sessions
from app.utils import escape_html, get_sender_data, build_progress_bar


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
    # Базовый лимит 35% от баланса
    balance_cap = max(1, int(user.reputation * 0.35))
    # Абсолютный максимум 50 000 печенек
    return min(user.reputation, 50000, balance_cap)


def _apply_luxury_tax(db: SupabaseDB, chat_id: int, bet: int) -> tuple[int, int]:
    """
    Если ставка > 5000, берем 10% налога с суммы превышения.
    Возвращает (чистая_ставка, налог).
    """
    if bet <= 5000:
        return bet, 0
    
    excess = bet - 5000
    tax = int(excess * 0.10)
    db.add_to_jackpot(chat_id, tax)
    return bet - tax, tax


def _risk_keyboard(user_id: int, win_amount: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛡 +25%", callback_data=f"dice_double_safe_{user_id}_{win_amount}"),
            InlineKeyboardButton(text="🃏 x2", callback_data=f"dice_double_double_{user_id}_{win_amount}"),
            InlineKeyboardButton(text="🔥 x3", callback_data=f"dice_double_triple_{user_id}_{win_amount}"),
        ]
    ])


async def run_mine(db: SupabaseDB, message: Message, sender, mode: str, *, edit: bool = False) -> None:
    """Логика шахты. Вынесена на уровень модуля для доступа из general_admin."""
    allowed, remaining = db.can_user_use_command(message.chat.id, sender.user_id, "mine", 90 * 60)
    if not allowed:
        text = f"⛏️ Шахта проветривается. Возвращайся через {remaining // 60 + 1} мин."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    if mode == "safe":
        reward = random.randint(12, 24)
        xp = random.randint(6, 10)
        title = "Безопасная выработка"
        text = "Ты копал(а) неглубоко и стабильно вынес(ла) печеньки."
    elif mode == "deep":
        reward = random.randint(4, 110)
        xp = random.randint(12, 24)
        title = "Глубокая шахта"
        text = "Глубоко, нервно, но без настоящего проигрыша."
    else:
        reward = random.randint(8, 60)
        xp = random.randint(8, 16)
        title = "Обычная смена"
        text = "Нормальная глубина, нормальная добыча."

    if random.random() < 0.08:
        bonus = random.randint(20, 45)
        reward += bonus
        text += f"\nБонусная жила: +<b>{bonus}</b> 🍪."

    updated = db.add_reputation(sender, reward) or sender
    db.add_xp(updated, xp)
    db.increment_stat(sender.id, "mine_plays") # Инкремент статистики шахты
    
    result = (
        f"⛏️ <b>{title}</b>\n\n"
        f"{text}\n"
        f"Режимы: <code>safe</code>, <code>normal</code>, <code>deep</code>\n"
        f"Награда: <b>{reward}</b> 🍪 и <b>{xp}</b> XP\n"
        f"Баланс: <b>{updated.reputation}</b> 🍪"
    )
    if edit:
        await message.edit_text(result, parse_mode="HTML")
    else:
        await message.answer(result, parse_mode="HTML")


def build_games_router(db: SupabaseDB, ai: AIService, bot_name: str) -> Router:
    router = Router(name="games")

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
                "Победа дает x2. Простая риск-игра, не фармилка.",
                parse_mode="HTML",
            )
            return

        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await deny_if_jailed(db, message, sender, "/coin"):
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

        # Налог на роскошь
        actual_bet, luxury_tax = _apply_luxury_tax(db, message.chat.id, bet)
        tax_note = f"\n<i>Уплачен налог на роскошь: {luxury_tax} 🍪</i>" if luxury_tax > 0 else ""

        player_side = aliases[choice]
        result_side = player_side if random.random() < 0.48 else ("tails" if player_side == "heads" else "heads")
        result_text = "орел" if result_side == "heads" else "решка"
        
        if player_side == result_side:
            win = actual_bet * 2
            new_balance = sender.reputation - bet + win
            db.update_user(sender.id, {"reputation": new_balance})
            await message.answer(
                f"🪙 Выпало: <b>{result_text}</b>\n"
                f"✅ {escape_html(sender.display_name)} выиграл <b>{win}</b> 🍪\n"
                f"Баланс: <b>{new_balance}</b> 🍪{tax_note}",
                parse_mode="HTML",
            )
        else:
            new_balance = sender.reputation - bet
            db.update_user(sender.id, {"reputation": new_balance})
            await message.answer(
                f"🪙 Выпало: <b>{result_text}</b>\n"
                f"❌ Ставка <b>{bet}</b> 🍪 сгорела.\n"
                f"Баланс: <b>{new_balance}</b> 🍪{tax_note}",
                parse_mode="HTML",
            )

    @router.message(Command("dice", "кубики", "кости"))
    async def dice_command(message: Message, command: CommandObject) -> None:
        bet, choice = _parse_bet_and_choice(command.args)
        if not bet or bet <= 0 or not choice:
            await message.answer(
                "🎲 <b>Кубики</b>\n\n"
                "Использование: <code>/dice &lt;ставка&gt; &lt;чет/нечет/дубль/2-12&gt;</code>\n"
                "• чет/нечет — выплата x2\n"
                "• дубль — выплата x5.5\n"
                "• точная сумма 2-12 — выплата от x5.5 до x32\n"
                "• после победы можно рискнуть и удвоить куш",
                parse_mode="HTML",
            )
            return

        normalized_choice = choice.replace("ё", "е").strip().lower()
        aliases = {
            "чет": "even",
            "четное": "even",
            "четная": "even",
            "чёт": "even",
            "чётное": "even",
            "чётная": "even",
            "even": "even",
            "e": "even",
            "нечет": "odd",
            "нечетное": "odd",
            "нечетная": "odd",
            "нечёт": "odd",
            "нечётное": "odd",
            "нечётная": "odd",
            "odd": "odd",
            "o": "odd",
            "дубль": "double",
            "дубли": "double",
            "double": "double",
            "doubles": "double",
            "pair": "double",
        }

        target_sum = None
        mode = aliases.get(normalized_choice)
        if mode is None and normalized_choice.isdigit():
            target_sum = int(normalized_choice)
            if 2 <= target_sum <= 12:
                mode = "sum"

        if mode is None:
            await message.answer(
                "🎲 <b>Кубики</b>\n\n"
                "Выбери: <code>чет</code>, <code>нечет</code>, <code>дубль</code> или сумму <code>2-12</code>.\n"
                "Пример: <code>/dice 100 нечет</code>",
                parse_mode="HTML",
            )
            return

        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await deny_if_jailed(db, message, sender, "/dice"):
            return
        if await _deny_bad_bet(message, sender, bet):
            return
        if sender.reputation < bet:
            await message.answer(f"❌ Не хватает печенек. Баланс: <b>{sender.reputation}</b> 🍪", parse_mode="HTML")
            return
        allowed, remaining = db.can_user_use_command(message.chat.id, sender.user_id, "dice", 20)
        if not allowed:
            await message.answer(f"⏳ Кубики еще катятся. Подожди {remaining} сек.")
            return

        # Налог на роскошь
        actual_bet, luxury_tax = _apply_luxury_tax(db, message.chat.id, bet)
        tax_note = f"\n<i>Уплачен налог на роскошь: {luxury_tax} 🍪</i>" if luxury_tax > 0 else ""

        die_1 = random.randint(1, 6)
        die_2 = random.randint(1, 6)
        total = die_1 + die_2
        is_double = die_1 == die_2
        is_even = total % 2 == 0

        labels = {
            "even": "чет",
            "odd": "нечет",
            "double": "дубль",
            "sum": f"сумма {target_sum}",
        }
        multipliers_by_sum = {
            2: 32.0,
            3: 16.0,
            4: 10.5,
            5: 8.0,
            6: 6.5,
            7: 5.5,
            8: 6.5,
            9: 8.0,
            10: 10.5,
            11: 16.0,
            12: 32.0,
        }

        if mode == "even":
            won = is_even
            multiplier = 2.0
        elif mode == "odd":
            won = not is_even
            multiplier = 2.0
        elif mode == "double":
            won = is_double
            multiplier = 5.5
        else:
            won = total == target_sum
            multiplier = multipliers_by_sum[target_sum or 7]

        result_line = f"🎲 Выпало: <b>{die_1}</b> + <b>{die_2}</b> = <b>{total}</b>"
        extra_line = "Пара на кубиках." if is_double else ("Сумма четная." if is_even else "Сумма нечетная.")
        if won:
            win = int(actual_bet * multiplier)
            new_balance = sender.reputation - bet + win
            db.update_user(sender.id, {"reputation": new_balance})
            sent = await message.answer(
                f"{result_line}\n"
                f"<i>{extra_line}</i>\n\n"
                f"✅ Ставка на <b>{labels[mode]}</b> сыграла: x{multiplier:g}\n"
                f"Выигрыш: <b>{win}</b> 🍪\n"
                f"Баланс: <b>{new_balance}</b> 🍪{tax_note}",
                reply_markup=_risk_keyboard(sender.user_id, win),
                parse_mode="HTML",
            )
            game_sessions.CASINO_DOUBLE_SESSIONS[f"{message.chat.id}_{sent.message_id}"] = {
                "user_id": sender.user_id,
                "win_amount": win,
            }
            return

        new_balance = sender.reputation - bet
        db.update_user(sender.id, {"reputation": new_balance})
        await message.answer(
            f"{result_line}\n"
            f"<i>{extra_line}</i>\n\n"
            f"❌ Ставка на <b>{labels[mode]}</b> не зашла.\n"
            f"Потеряно: <b>{bet}</b> 🍪\n"
            f"Баланс: <b>{new_balance}</b> 🍪{tax_note}",
            parse_mode="HTML",
        )

    @router.message(Command("diceduel", "кубодуэль", "дайсдуэль"))
    async def dice_duel_command(message: Message, command: CommandObject) -> None:
        if not message.reply_to_message or not message.reply_to_message.from_user:
            await message.answer(
                "🎲 <b>Дуэль на кубиках</b>\n\n"
                "Ответь на сообщение соперника: <code>/diceduel сумма</code>\n"
                "Оба игрока кидают 2d6. У кого сумма выше — забирает ставку.",
                parse_mode="HTML",
            )
            return
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Использование: ответом на игрока <code>/diceduel сумма</code>", parse_mode="HTML")
            return

        bet = int(command.args.strip())
        target_sender = get_sender_data(message.reply_to_message)
        if target_sender.is_bot or target_sender.user_id == message.from_user.id:
            await message.answer("❌ Дуэль нужна между двумя живыми игроками.")
            return

        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        target = db.get_or_create_user(message.chat.id, target_sender)
        if await deny_if_jailed(db, message, sender, "/diceduel"):
            return
        if jail_remaining(db, target):
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

        allowed, remaining = db.can_user_use_command(message.chat.id, sender.user_id, "diceduel", 20)
        if not allowed:
            await message.answer(f"⏳ Подожди {remaining} сек. перед новой кубодуэлью.")
            return

        key = f"{abs(message.chat.id) % 1_000_000}{sender.user_id % 1_000_000}{target.user_id % 1_000_000}{random.randint(100, 999)}"
        game_sessions.DICE_DUEL_SESSIONS[key] = {
            "chat_id": message.chat.id,
            "challenger_id": sender.user_id,
            "target_id": target.user_id,
            "bet": bet,
            "created_at": time.time(),
        }
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Принять бросок", callback_data=f"diceduel_{key}_accept")],
            [InlineKeyboardButton(text="Отказаться", callback_data=f"diceduel_{key}_decline")],
        ])
        await message.answer(
            f"🎲 <b>Кубодуэль на {bet} 🍪</b>\n\n"
            f"{escape_html(sender.display_name)} вызывает {escape_html(target.display_name)}.\n"
            f"У соперника 15 минут на ответ.",
            reply_markup=keyboard,
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
                "Победа дает x2, ничья возвращает ставку.",
                parse_mode="HTML",
            )
            return

        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await deny_if_jailed(db, message, sender, "/rps"):
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
            win = bet * 2
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
        if await deny_if_jailed(db, message, sender, "/duel"):
            return
        if jail_remaining(db, target):
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
        game_sessions.DUEL_SESSIONS[key] = {
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

    @router.message(Command("fish", "рыбалка"))
    async def fish_command(message: Message) -> None:
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await deny_if_jailed(db, message, sender, "/fish"):
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

    @router.message(Command("box", "коробка", "сундук"))
    async def box_command(message: Message) -> None:
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await deny_if_jailed(db, message, sender, "/box"):
            return
        allowed, remaining = db.can_user_use_command(message.chat.id, sender.user_id, "box", 60 * 60)
        if not allowed:
            await message.answer(f"📦 Коробка Ники ещё закрыта. Возвращайся через {remaining // 60 + 1} мин.")
            return

        roll = random.random()
        if roll < 0.50:
            reward, xp, title, text = random.randint(7, 16), random.randint(4, 8), "Обычная коробка", "Внутри крошки, но честные."
        elif roll < 0.82:
            reward, xp, title, text = random.randint(18, 34), random.randint(8, 14), "Хорошая коробка", "Ника спрятала туда маленький запас."
        elif roll < 0.97:
            reward, xp, title, text = random.randint(55, 95), random.randint(16, 26), "Редкая коробка", "Под крышкой внезапно блеснул приличный куш."
        else:
            reward, xp, title, text = random.randint(150, 260), random.randint(30, 48), "Легендарная коробка", "Она была тяжёлая не просто так."

        updated = db.add_reputation(sender, reward) or sender
        db.add_xp(updated, xp)
        await message.answer(
            f"📦 <b>{title}</b>\n\n"
            f"{text}\n"
            f"Награда: <b>{reward}</b> 🍪 и <b>{xp}</b> XP\n"
            f"Баланс: <b>{updated.reputation}</b> 🍪",
            parse_mode="HTML",
        )

    @router.message(Command("scratch", "скретч", "лотерейка"))
    async def scratch_command(message: Message) -> None:
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await deny_if_jailed(db, message, sender, "/scratch"):
            return
        allowed, remaining = db.can_user_use_command(message.chat.id, sender.user_id, "scratch", 35 * 60)
        if not allowed:
            await message.answer(f"🎟 Новая скретч-карта будет через {remaining // 60 + 1} мин.")
            return

        symbols = ["🍪", "🍒", "⭐", "💎", "🎲", "🧁"]
        card = [random.choice(symbols) for _ in range(3)]
        counts = {symbol: card.count(symbol) for symbol in symbols}
        best = max(counts.values())
        jackpot_symbol = card[0] if best == 3 else ""
        if best == 3 and jackpot_symbol == "💎":
            reward, xp, result = random.randint(180, 320), random.randint(28, 45), "💎 Три бриллианта. Очень жирная карта."
        elif best == 3:
            reward, xp, result = random.randint(90, 170), random.randint(20, 34), "✨ Три одинаковых символа."
        elif best == 2:
            reward, xp, result = random.randint(24, 55), random.randint(9, 17), "🥈 Два совпадения."
        else:
            reward, xp, result = random.randint(5, 12), random.randint(3, 7), "🧾 Совпадений нет, но карта не пустая."

        updated = db.add_reputation(sender, reward) or sender
        db.add_xp(updated, xp)
        await message.answer(
            f"🎟 <b>Скретч-карта</b>\n\n"
            f"<code>[ {card[0]} | {card[1]} | {card[2]} ]</code>\n"
            f"{result}\n"
            f"Награда: <b>{reward}</b> 🍪 и <b>{xp}</b> XP\n"
            f"Баланс: <b>{updated.reputation}</b> 🍪",
            parse_mode="HTML",
        )

    @router.message(Command("mine", "шахта", "копать"))
    async def mine_command(message: Message, command: CommandObject) -> None:
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await deny_if_jailed(db, message, sender, "/mine"):
            return

        raw_mode = (command.args or "").strip().lower().replace("ё", "е")
        if not raw_mode:
            await message.answer(
                "⛏ <b>Шахта печенек</b>\n\n"
                "Выбери глубину. Проигрыша нет, отличается только разброс награды:\n"
                "• <b>safe</b> — стабильно <b>12-24</b> 🍪 и <b>6-10</b> XP\n"
                "• <b>normal</b> — средний риск разброса <b>8-60</b> 🍪 и <b>8-16</b> XP\n"
                "• <b>deep</b> — может быть мало, может жирно: <b>4-110</b> 🍪 и <b>12-24</b> XP\n\n"
                "Можно писать сразу: <code>/mine deep</code>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🟢 Safe", callback_data=f"mine_safe_{sender.user_id}"),
                        InlineKeyboardButton(text="🟡 Normal", callback_data=f"mine_normal_{sender.user_id}"),
                        InlineKeyboardButton(text="🔴 Deep", callback_data=f"mine_deep_{sender.user_id}"),
                    ]
                ]),
                parse_mode="HTML",
            )
            return

        aliases = {
            "safe": "safe", "легко": "safe", "безопасно": "safe",
            "normal": "normal", "норм": "normal", "обычно": "normal",
            "deep": "deep", "глубоко": "deep", "глубже": "deep",
        }
        mode = aliases.get(raw_mode)
        if not mode:
            await message.answer("⛏ Выбери режим: <code>/mine safe</code>, <code>/mine normal</code> или <code>/mine deep</code>", parse_mode="HTML")
            return

        await run_mine(db, message, sender, mode)

        if mode == "safe":
            reward = random.randint(12, 24)
            xp = random.randint(6, 10)
            title = "Безопасная выработка"
            text = "Ты копал(а) неглубоко и стабильно вынес(ла) печеньки."
        elif mode == "deep":
            reward = random.randint(4, 110)
            xp = random.randint(12, 24)
            title = "Глубокая шахта"
            text = "Глубоко, нервно, но без настоящего проигрыша."
        else:
            reward = random.randint(8, 60)
            xp = random.randint(8, 16)
            title = "Обычная смена"
            text = "Нормальная глубина, нормальная добыча."

        if random.random() < 0.08:
            bonus = random.randint(20, 45)
            reward += bonus
            text += f"\nБонусная жила: +<b>{bonus}</b> 🍪."

        updated = db.add_reputation(sender, reward) or sender
        db.add_xp(updated, xp)
        result = (
            f"⛏ <b>{title}</b>\n\n"
            f"{text}\n"
            f"Режимы: <code>safe</code>, <code>normal</code>, <code>deep</code>\n"
            f"Награда: <b>{reward}</b> 🍪 и <b>{xp}</b> XP\n"
            f"Баланс: <b>{updated.reputation}</b> 🍪"
        )
        if edit:
            await message.edit_text(result, parse_mode="HTML")
        else:
            await message.answer(result, parse_mode="HTML")

    @router.message(Command("dailyquest", "квест", "дейлик"))
    async def dailyquest_command(message: Message) -> None:
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await deny_if_jailed(db, message, sender, "/dailyquest"):
            return
        allowed, remaining = db.can_user_use_command(message.chat.id, sender.user_id, "dailyquest", 24 * 60 * 60)
        if not allowed:
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            await message.answer(f"📜 Ежедневный квест уже закрыт. Новый через {hours} ч {minutes + 1} мин.")
            return

        quests = [
            ("Починить шумный чат", "Ты подкрутил(а) пару невидимых винтиков, и чат стал чуть менее хаотичным."),
            ("Доставить печеньки", "Маршрут был странный, но коробка доехала почти целой."),
            ("Помочь Нике с багом", "Баг притворялся фичей, но ты его раскусил(а)."),
            ("Разобрать архив мемов", "Половина архива оказалась опасной для психики, зато награда настоящая."),
            ("Проверить склад", "На складе нашлись забытые печеньки и подозрительно довольная тишина."),
        ]
        title, text = random.choice(quests)
        reward = random.randint(65, 145)
        xp = random.randint(24, 52)
        if random.random() < 0.10:
            reward += random.randint(45, 90)
            text += "\nНика добавила премию за красивое исполнение."

        updated = db.add_reputation(sender, reward) or sender
        level_result = db.add_xp(updated, xp)
        level_line = f"\nНовый уровень: <b>{level_result['new_level']}</b>!" if level_result.get("level_up") else ""
        await message.answer(
            f"📜 <b>Ежедневный квест: {escape_html(title)}</b>\n\n"
            f"{escape_html(text)}\n"
            f"Награда: <b>{reward}</b> 🍪 и <b>{xp}</b> XP{level_line}\n"
            f"Баланс: <b>{updated.reputation}</b> 🍪",
            parse_mode="HTML",
        )

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
        if await deny_if_jailed(db, message, sender, "/casino"):
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

        # Налог на роскошь
        actual_bet, luxury_tax = _apply_luxury_tax(db, message.chat.id, bet)
        tax_note = f"\n<i>Уплачен налог на роскошь: {luxury_tax} 🍪</i>" if luxury_tax > 0 else ""

        # Базовый налог в джекпот (1% от чистой ставки)
        chat_settings = db.get_chat_settings(message.chat.id)
        current_jackpot = chat_settings.casino_jackpot
        base_tax = max(1, actual_bet // 100)
        db.update_user(sender.id, {"reputation": sender.reputation - bet})
        db.add_to_jackpot(message.chat.id, base_tax)
        current_jackpot += base_tax

        # Символы слотов
        symbols = ["🍒", "🍋", "🍇", "🍉", "🔔", "💎", "🎰"]
        r1, r2, r3 = random.choice(symbols), random.choice(symbols), random.choice(symbols)
        symbol_rules = {
            # pair — множитель возврата (>1.0 = прибыль, <1.0 = частичный возврат)
            "🍒": {"name": "Вишневый ряд",        "triple": 4.5,  "pair": 1.10},
            "🍋": {"name": "Лимонная линия",       "triple": 4.5,  "pair": 1.10},
            "🍇": {"name": "Виноградный сбор",     "triple": 6.0,  "pair": 1.20},
            "🍉": {"name": "Арбузный куш",         "triple": 7.5,  "pair": 1.30},
            "🔔": {"name": "Колокольный звон",     "triple": 11.0, "pair": 1.70},
            "💎": {"name": "Бриллиантовая линия",  "triple": 17.0, "pair": 2.10},
            "🎰": {"name": "Легендарный слот",     "triple": 32.0, "pair": 2.50},
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
                jackpot_bonus = min(current_jackpot, actual_bet * 8)
                win_total = int(actual_bet * symbol_rules[r1]["triple"]) + jackpot_bonus
                db.update_chat_settings(message.chat.id, casino_jackpot=500)
                result_text = (
                    f"🌌 <b>ЛЕГЕНДАРНЫЙ ДЖЕКПОТ!!!</b>\n\n"
                    f"Выигрыш x35 и джекпот-бонус <b>{jackpot_bonus}</b> 🍪!\n"
                    f"Итого: <b>{win_total}</b> 🍪 🎉🍾"
                )
                # Глобальное оповещение
                await message.answer(
                    f"🎊 <b>ВНИМАНИЕ ВСЕМ!</b> 🎊\n\n"
                    f"Игрок <b>{escape_html(sender.display_name)}</b> только что сорвал "
                    f"<b>ГЛОБАЛЬНЫЙ ДЖЕКПОТ</b> и забрал <b>{win_total} 🍪</b>!\n"
                    f"Поздравляем счастливчика! 🥳🥂"
                )
            else:
                multiplier = symbol_rules[r1]["triple"]
                win_total = int(actual_bet * multiplier)
                result_text = f"✨ <b>{symbol_rules[r1]['name'].upper()}!</b> Три в ряд x{multiplier:g}!"
        elif pair_symbol:
            multiplier = symbol_rules[pair_symbol]["pair"]
            win_total = max(1, int(actual_bet * multiplier))
            result_text = f"🥂 <b>ПАРА: {pair_symbol}{pair_symbol}</b> {symbol_rules[pair_symbol]['name']} x{multiplier:g}"
        elif set(reels).issubset(fruit_symbols):
            # Три разных фрукта — маленький утешительный возврат
            win_total = max(1, int(actual_bet * 0.90))
            result_text = "🍹 <b>ФРУКТОВЫЙ МИКС!</b> Три разных фрукта — возврат x0.90"
        elif set(reels).issubset(premium_symbols):
            # Три разных дорогих — хорошая комбинация
            win_total = int(actual_bet * 1.50)
            result_text = "⚡ <b>ПРЕМИУМ-ЛИНИЯ!</b> Три разных дорогих символа x1.50"
        elif sum(1 for symbol in reels if symbol in premium_symbols) >= 2:
            # Два дорогих + один дешевый — частичный возврат
            win_total = max(1, int(actual_bet * 0.90))
            result_text = "💠 <b>ПОЧТИ ПРЕМИУМ!</b> Два дорогих символа — возврат x0.90"
        elif sum(1 for symbol in reels if symbol in fruit_symbols) >= 2:
            # Два фруктовых + один дорогой — крошки
            win_total = max(1, int(actual_bet * 0.85))
            result_text = "🍬 <b>СЛАДКИЙ МИКС!</b> Два фруктовых — крошки x0.85"
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
            f"💰 Твой баланс: <b>{new_bal}</b> 🍪{tax_note}"
        )
        
        keyboard = None
        if win_total > 0 and not is_jackpot:
            keyboard = _risk_keyboard(sender.user_id, win_total)

        await msg.edit_text(final_msg, reply_markup=keyboard, parse_mode="HTML")
        if win_total > 0 and not is_jackpot:
            game_sessions.CASINO_DOUBLE_SESSIONS[f"{message.chat.id}_{msg.message_id}"] = {
                "user_id": sender.user_id,
                "win_amount": win_total,
            }

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
        if await deny_if_jailed(db, message, sender, "/tower"):
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
            game_sessions.TOWER_SESSIONS[f"{message.chat.id}_{sent_msg.message_id}"] = {
                "user_id": sender.user_id,
                "floor": 1,
                "bet": bet,
                "weather_idx": weathers.index(weather),
            }

    
    return router


