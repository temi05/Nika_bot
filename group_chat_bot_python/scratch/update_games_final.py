import os

target_file = r"c:\Users\Темирлан\.gemini\antigravity\playground\Nika\group_chat_bot_python\app\bot\commands.py"

with open(target_file, "r", encoding="utf-8") as f:
    content = f.read()

start_marker = '@router.message(Command("casino", "gamble", "spin", "казино", "ставка"))'
start_idx = content.find(start_marker)

new_content = content[:start_idx] + '''@router.message(Command("casino", "gamble", "spin", "казино", "ставка"))
    async def casino_command(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("🎰 <b>Казино NeuroNika</b>\\n\\nИспользование: <code>/casino &lt;сумма&gt;</code>\\nМинимальная ставка: <code>1 🍪</code>", parse_mode="HTML")
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
                f"⏳ <b>Автомат остывает.</b>\\nПодожди ещё <code>{remaining} сек.</code>",
                parse_mode="HTML"
            )
            return

        if sender.reputation < bet:
            await message.answer(f"❌ Недостаточно печенек! У тебя всего: <b>{sender.reputation}</b> 🍪")
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
        
        # Анимация "Саспенс"
        msg = await message.answer(f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\\n💰 Ставка: <code>{bet}</code> 🍪\\n🏆 Джекпот: <code>{current_jackpot}</code>\\n\\n[ 🎲 | 🎲 | 🎲 ]", parse_mode="HTML")
        await asyncio.sleep(0.5)
        
        await msg.edit_text(f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\\n💰 Ставка: <code>{bet}</code> 🍪\\n🏆 Джекпот: <code>{current_jackpot}</code>\\n\\n[ {r1} | 🎲 | 🎲 ]", parse_mode="HTML")
        await asyncio.sleep(0.5)
        
        await msg.edit_text(f"🎰 <b>ИГРОК: {escape_html(sender.display_name)}</b>\\n💰 Ставка: <code>{bet}</code> 🍪\\n🏆 Джекпот: <code>{current_jackpot}</code>\\n\\n[ {r1} | {r2} | 🎲 ]", parse_mode="HTML")
        
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

        if r1 == r2 == r3:
            if r1 == "🎰":
                is_jackpot = True
                win_total = int(bet * 50) + current_jackpot
                db.update_chat_settings(message.chat.id, casino_jackpot=500)
                result_text = f"🌌 <b>ЛЕГЕНДАРНЫЙ ДЖЕКПОТ!!!</b>\\n\\nТы сорвал куш в <b>{win_total}</b> 🍪! 🎉🍾"
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
            result_text = f"💨 <b>НЕУДАЧА!</b> В следующий раз повезет!\\n<i>(Утешительный приз: +{consol_xp} XP)</i>"

        new_bal = (sender.reputation - bet) + win_total
        if win_total > 0:
            db.update_user(sender.id, {"reputation": new_bal})
        else:
            db.update_user(sender.id, {"reputation": new_bal, "xp": sender.xp + consol_xp})

        final_msg = (
            f"🎰 <b>РЕЗУЛЬТАТЫ СПИНА</b>\\n"
            f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\\n\\n"
            f"<code>{final_symbols}</code>\\n\\n"
            f"{result_text}\\n"
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
                f"🃏 <b>РИСК ОПРАВДАН!</b>\\n\\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\\n"
                f"🔥 Ты удвоил куш: <b>{new_win}</b> 🍪!\\n"
                f"📈 Баланс: <b>{sender.reputation - win_amount + new_win}</b> 🍪",
                parse_mode="HTML"
            )
            await query.answer("💰 Удвоено!")
        else:
            await query.message.edit_text(
                f"🃏 <b>РИСК НЕ ОПРАВДАН...</b>\\n\\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\\n"
                f"💀 Ты потерял всё: <b>{win_amount}</b> 🍪\\n"
                f"📉 Баланс: <b>{sender.reputation - win_amount}</b> 🍪",
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
        multipliers = [1.0, 1.2, 1.5, 2.0, 3.0, 5.0, 10.0, 25.0, 50.0, 100.0]
        if floor < 1: return 1.0
        if floor > 10: return 100.0
        return multipliers[floor-1]

    # --- ИГРА: БАШНЯ ФОРТУНЫ (COMPACT & DYNAMIC VERSION) ---
    _tower_locks = {}

    @router.message(Command("tower", "башня", "climb"))
    async def tower_command(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer(
                "🏰 <b>Башня Фортуны</b>\\n\\n"
                "🛡 <b>Чекпоинты:</b> 5 эт. (сохранение 30%), 8 эт. (60%)\\n"
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

        weathers = [
            {"name": "☀️ Ясно", "chance_mod": 0, "reward_mod": 1.0},
            {"name": "🌫 Туман", "chance_mod": -0.05, "reward_mod": 1.2},
            {"name": "🌪 Буря", "chance_mod": -0.15, "reward_mod": 1.5},
            {"name": "🌈 Попутный ветер", "chance_mod": 0.1, "reward_mod": 0.9}
        ]
        weather = random.choice(weathers)
        
        db.update_user(sender.id, {"reputation": sender.reputation - bet})
        await _show_tower(message, floor=1, bet=bet, user_id=sender.user_id, is_new=True, weather=weather, event_msg="Ты у подножия башни.")

    @router.callback_query(F.data.startswith("tower_"))
    async def tower_callback(query: CallbackQuery) -> None:
        parts = query.data.split("_")
        action = parts[1]
        floor = int(parts[2])
        bet = int(parts[3])
        original_user_id = int(parts[4])
        weather_idx = int(parts[5]) if len(parts) > 5 else 0
        
        weathers = [
            {"name": "☀️ Ясно", "chance_mod": 0, "reward_mod": 1.0},
            {"name": "🌫 Туман", "chance_mod": -0.05, "reward_mod": 1.2},
            {"name": "🌪 Буря", "chance_mod": -0.15, "reward_mod": 1.5},
            {"name": "🌈 Попутный ветер", "chance_mod": 0.1, "reward_mod": 0.9}
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
                f"🎉 <b>ТЫ ЗАБРАЛ ПРИЗ!</b>\\n\\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\\n"
                f"🪜 Достигнут этаж: <b>{floor}</b>\\n"
                f"💰 Выигрыш: <b>{win} 🍪</b>\\n\\n"
                f"📈 Баланс: <b>{sender.reputation + win}</b> 🍪",
                parse_mode="HTML"
            )
            await query.answer("💰 Приз в кармане!")
            
        elif action == "up":
            chances = [1.0, 0.9, 0.85, 0.8, 0.75, 0.7, 0.6, 0.5, 0.4, 0.3]
            base_chance = chances[floor-1] if floor <= len(chances) else 0.2
            success_chance = max(0.1, base_chance + weather["chance_mod"])
            
            # Случайные события (Random Encounters)
            event_msg = ""
            if random.random() < 0.15: # 15% шанс на особое событие
                events = [
                    ("🎁 Нашел старую заначку! (+10 🍪)", lambda u: db.update_user(u.id, {"reputation": u.reputation + 10})),
                    ("🪤 Наступил на ловушку! (-5 🍪)", lambda u: db.update_user(u.id, {"reputation": max(0, u.reputation - 5)})),
                    ("🔮 Загадочная аура...", lambda u: None)
                ]
                ev_text, ev_action = random.choice(events)
                event_msg = ev_text
                ev_action(sender)

            if random.random() < success_chance:
                new_floor = floor + 1
                if new_floor > 10:
                    multiplier = _get_tower_multiplier(10) * weather["reward_mod"]
                    win = int(bet * multiplier)
                    db.update_user(sender.id, {"reputation": sender.reputation + win})
                    await query.message.edit_text(f"🏆 <b>ТЫ ПОКОРИЛ ВЕРШИНУ БАШНИ!</b> (x{multiplier:.1f})\\n💰 Твой куш: {win} 🍪", parse_mode="HTML")
                    return

                try:
                    await _show_tower(query.message, floor=new_floor, bet=bet, user_id=original_user_id, is_new=False, weather=weather, event_msg=event_msg)
                    await query.answer("✅ Успешный подъем!")
                except Exception: pass
            else:
                # Чекпоинты
                safe_win = 0
                mult = _get_tower_multiplier(floor) * weather["reward_mod"]
                if floor >= 8: safe_win = int(bet * mult * 0.6)
                elif floor >= 5: safe_win = int(bet * mult * 0.3)

                if safe_win > 0:
                    db.update_user(sender.id, {"reputation": sender.reputation + safe_win})

                await query.message.edit_text(
                    f"💀 <b>ТЫ СОРВАЛСЯ ВНИЗ!</b>\\n\\n"
                    f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\\n"
                    f"🪜 Упал на <b>{floor + 1}</b> этаже.\\n"
                    f"🛡 Сработала страховка: <b>{safe_win}</b> 🍪\\n\\n"
                    f"📈 Баланс: <b>{sender.reputation + safe_win}</b> 🍪",
                    parse_mode="HTML"
                )
                await query.answer("💥 БА-БАХ!", show_alert=True)

    async def _show_tower(msg: Message, floor: int, bet: int, user_id: int, is_new: bool, weather: dict, event_msg: str = "") -> None:
        multiplier = _get_tower_multiplier(floor) * weather["reward_mod"]
        weathers_list = ["☀️ Ясно", "🌫 Туман", "🌪 Буря", "🌈 Попутный ветер"]
        w_idx = weathers_list.index(weather["name"]) if weather["name"] in weathers_list else 0

        # Компактный Радар (показываем только 3 этажа)
        tower_lines = []
        for i in range(min(10, floor + 1), max(0, floor - 2), -1):
            m = _get_tower_multiplier(i) * weather["reward_mod"]
            m_text = f"x{m:.1f}" if weather["name"] != "🌫 Туман" or i <= floor else "x???"
            
            if i == floor:
                line = f"▶️ <b>[{i:02}] {m_text} 🏃 ТЫ ТУТ</b>"
            elif i < floor:
                line = f"✅ <code>[{i:02}] {m_text}</code>"
            else:
                prefix = "🪜" if i not in (5, 8, 10) else ("🥉" if i==5 else "🥈" if i==8 else "💎")
                line = f"{prefix} <code>[{i:02}] {m_text}</code>"
            tower_lines.append(line)

        progress = "▰" * floor + "▱" * (10 - floor)
        event_block = f"\\n<i>{event_msg}</i>\\n" if event_msg else ""
        
        text = (
            f"🏰 <b>БАШНЯ ФОРТУНЫ</b>\\n"
            f"🌤 Погода: <b>{weather['name']}</b>\\n"
            f"<code>{progress}</code> {floor}/10\\n"
            f"{event_block}\\n"
            + "\\n".join(tower_lines) + "\\n\\n" +
            f"💰 Ставка: <code>{bet}</code> 🍪 | 💵 Текущий куш: <b>{int(bet * multiplier)} 🍪</b>\\n"
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⏫ ЛЕЗТЬ ДАЛЬШЕ", callback_data=f"tower_up_{floor}_{bet}_{user_id}_{w_idx}"),
                InlineKeyboardButton(text="💰 ЗАБРАТЬ", callback_data=f"tower_take_{floor}_{bet}_{user_id}_{w_idx}")
            ]
        ])
        
        if is_new: await msg.answer(text, reply_markup=kb, parse_mode="HTML")
        else: await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")

    return router
'''

with open(target_file, "w", encoding="utf-8") as f:
    f.write(new_content)

print("Update complete")
