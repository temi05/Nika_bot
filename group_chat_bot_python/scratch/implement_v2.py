
import os

file_path = 'app/bot/commands.py'

# 1. NEW CASINO LOGIC
casino_code = r'''
    @router.message(Command("casino", "gamble", "spin", "казино", "ставка"))
    async def casino_command(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Использование: <code>/casino <ставка></code>", parse_mode="HTML")
            return
            
        bet = int(command.args.strip())
        sender_data = get_sender_data(message)
        sender = db.get_or_create_user(message.chat.id, sender_data)
        
        allowed, remaining = db.can_use_command(message.chat.id, f"casino_{sender.user_id}", 10)
        if not allowed:
            await message.answer(f"⏳ Казино на перезагрузке. Подожди {remaining} сек.")
            return

        if bet < 1:
            await message.answer("❌ Минимальная ставка: 1 🍪")
            return
            
        if sender.reputation < bet:
            await message.answer(f"❌ Недостаточно печенек! У тебя: {sender.reputation} 🍪")
            return

        # Снимаем ставку и обновляем джекпот (1% от ставки)
        chat_settings = db.get_chat_settings(message.chat.id)
        jackpot_add = max(1, int(bet * 0.01))
        new_jackpot = chat_settings.casino_jackpot + jackpot_add
        db.update_chat_settings(message.chat.id, casino_jackpot=new_jackpot)
        db.update_user(sender.id, {"reputation": sender.reputation - bet})
        
        symbols = ["🍎", "🍋", "🍒", "💎", "🎰"]
        msg = await message.answer(f"🎰 <b>{escape_html(sender.display_name)}</b> крутит слоты...\n[ ⏳ | ⏳ | ⏳ ]", parse_mode="HTML")
        
        # Анимация
        for _ in range(3):
            temp_res = [random.choice(symbols) for _ in range(3)]
            await msg.edit_text(
                f"🎰 <b>{escape_html(sender.display_name)}</b> крутит слоты...\n"
                f"💰 Джекпот: <b>{new_jackpot}</b> 🍪\n"
                f"[ {temp_res[0]} | {temp_res[1]} | {temp_res[2]} ]", 
                parse_mode="HTML"
            )
            await asyncio.sleep(0.6)
            
        result = [random.choice(symbols) for _ in range(3)]
        win_multiplier = 0
        jackpot_won = False
        
        if result[0] == result[1] == result[2]:
            if result[0] == "🎰": 
                win_multiplier = 10
                jackpot_won = True
            elif result[0] == "💎": win_multiplier = 7
            elif result[0] == "🍒": win_multiplier = 5
            elif result[0] == "🍋": win_multiplier = 3
            else: win_multiplier = 2
        elif result[0] == result[1] or result[1] == result[2]:
            win_multiplier = 1.5
            
        final_win = int(bet * win_multiplier)
        bonus_text = ""
        
        if jackpot_won:
            final_win += new_jackpot
            bonus_text = f"\n\n🔥 <b>ГРАНДИОЗНЫЙ ВЫИГРЫШ!</b>\nТы забрал Глобальный Джекпот: <b>{new_jackpot}</b> 🍪!"
            db.update_chat_settings(message.chat.id, casino_jackpot=500) # Сброс до базы
        
        if final_win > 0:
            db.update_user(sender.id, {"reputation": sender.reputation + final_win})
            res_text = (
                f"🎰 <b>РЕЗУЛЬТАТ</b>\n"
                f"💰 Джекпот: <b>{new_jackpot if not jackpot_won else 500}</b> 🍪\n"
                f"[ {result[0]} | {result[1]} | {result[2]} ]\n\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                f"✅ Выигрыш: <b>{final_win}</b> 🍪 (x{win_multiplier}){bonus_text}\n"
                f"📈 Баланс: <b>{sender.reputation - bet + final_win}</b> 🍪"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🃏 Рискнуть: Удвоить!", callback_data=f"dice_double_{sender.user_id}_{final_win}")]
            ])
            await msg.edit_text(res_text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.edit_text(
                f"🎰 <b>РЕЗУЛЬТАТ</b>\n"
                f"💰 Джекпот: <b>{new_jackpot}</b> 🍪\n"
                f"[ {result[0]} | {result[1]} | {result[2]} ]\n\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                f"❌ Проигрыш: <b>{bet}</b> 🍪\n"
                f"📈 Баланс: <b>{sender.reputation - bet}</b> 🍪",
                parse_mode="HTML"
            )
'''

# 2. NEW TOWER LOGIC
tower_code = r'''
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
            {"name": "🌫 Туман", "chance_mod": 0, "reward_mod": 1.2, "desc": "Множители скрыты, но награда +20%!"},
            {"name": "🌪 Шторм", "chance_mod": -0.15, "reward_mod": 1.5, "desc": "Трудно лезть, но награда x1.5!"},
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
            {"name": "🌫 Туман", "chance_mod": 0, "reward_mod": 1.2},
            {"name": "🌪 Шторм", "chance_mod": -0.15, "reward_mod": 1.5},
            {"name": "🌈 Радуга", "chance_mod": 0.1, "reward_mod": 0.9}
        ]
        weather = weathers[weather_idx]

        if query.from_user.id != original_user_id:
            await query.answer("❌ Не твой подъем!", show_alert=True)
            return
            
        now = time.time()
        lock_key = f"{query.message.chat.id}_{query.message.message_id}"
        if lock_key in _tower_locks and now - _tower_locks[lock_key] < 1.1:
            await query.answer("⏳ Не спеши!", show_alert=False)
            return
        _tower_locks[lock_key] = now

        sender = db.get_user_by_platform_id(query.message.chat.id, original_user_id)
        if not sender: return

        if action == "take":
            multiplier = _get_tower_multiplier(floor) * weather["reward_mod"]
            win = int(bet * multiplier)
            db.update_user(sender.id, {"reputation": sender.reputation + win})
            
            await query.message.edit_text(
                f"🎉 <b>ПОБЕДА!</b>\n\n"
                f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                f"🌈 Погода: <b>{weather['name']}</b>\n"
                f"🪜 Этажей: <b>{floor}</b>\n"
                f"💰 Куш: <b>{win} 🍪</b>\n\n"
                f"📈 Баланс: <b>{sender.reputation + win}</b> 🍪",
                parse_mode="HTML"
            )
            await query.answer("💰 Забрал!")
            
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
                    await query.message.edit_text(f"🏆 <b>ВЕРШИНА!</b> (x{multiplier})\n💰 Выигрыш: {win} 🍪", parse_mode="HTML")
                    return

                try:
                    await _show_tower(query.message, floor=new_floor, bet=bet, user_id=original_user_id, is_new=False, weather=weather)
                    await query.answer("✅ Выше!")
                except Exception: pass
            else:
                safe_win = 0
                mult = _get_tower_multiplier(floor) * weather["reward_mod"]
                if floor >= 8: safe_win = int(bet * mult * 0.6)
                elif floor >= 5: safe_win = int(bet * mult * 0.3)

                if safe_win > 0:
                    db.update_user(sender.id, {"reputation": sender.reputation + safe_win})

                await query.message.edit_text(
                    f"💀 <b>ПАДЕНИЕ!</b>\n"
                    f"👤 Игрок: <b>{escape_html(sender.display_name)}</b>\n"
                    f"🪜 Сорвался на {floor + 1} эт.\n"
                    f"🛡 Сейф: {safe_win} 🍪\n"
                    f"📈 Баланс: <b>{sender.reputation + safe_win}</b> 🍪",
                    parse_mode="HTML"
                )
                await query.answer("💥 Упал!", show_alert=True)

    async def _show_tower(msg: Message, floor: int, bet: int, user_id: int, is_new: bool, weather: dict) -> None:
        multiplier = _get_tower_multiplier(floor) * weather["reward_mod"]
        next_mult = _get_tower_multiplier(floor + 1) * weather["reward_mod"]
        
        weathers_list = ["☀️ Солнечно", "🌫 Туман", "🌪 Шторм", "🌈 Радуга"]
        w_idx = 0
        try: w_idx = weathers_list.index(weather["name"])
        except: pass

        tower_lines = []
        for i in range(10, 0, -1):
            m = _get_tower_multiplier(i) * weather["reward_mod"]
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
'''

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace Casino
casino_start_marker = '@router.message(Command("casino"'
casino_start = content.find(casino_start_marker)
if casino_start != -1:
    casino_end = content.find('@router.callback_query(F.data.startswith("dice_double_"))', casino_start)
    if casino_end != -1:
        content = content[:casino_start] + casino_code.strip() + "\n\n" + content[casino_end:]

# Replace Tower
tower_start_marker = '# --- ИГРА: БАШНЯ ФОРТУНЫ'
tower_start = content.find(tower_start_marker)
if tower_start != -1:
    tower_end = content.find('return router', tower_start)
    if tower_end != -1:
        content = content[:tower_start] + tower_code.strip() + "\n\n    " + content[tower_end:]

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Casino and Tower logic updated safely!")
