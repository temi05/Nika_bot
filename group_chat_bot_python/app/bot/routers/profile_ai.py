import base64
import random
import time
from io import BytesIO

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from app.models import MemoryRecord
from app.services.ai_service import AIService
from app.services.supabase_db import SupabaseDB
from app.bot.admin import is_admin
from app.bot.routers.jail_helpers import deny_if_jailed
from app.utils import build_progress_bar, escape_html, get_sender_data, parse_birthday_parts

NIKA_REFERENCE_ASSET_KEY = "nika_reference"

def build_profile_ai_router(db: SupabaseDB, ai: AIService, bot_name: str) -> Router:
    router = Router(name="profile_ai")
    
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
        facts = await ai.memory.get_relevant_facts(message.chat.id, target.display_name, target.display_name, target.user_id)
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
        
        record = MemoryRecord(
            fact=f"{sender.display_name}: {fact[:500]}",
            source="manual_memory",
            confidence=0.98,
            meta={"user_id": sender.user_id, "user_name": sender.display_name},
            entity_user_id=sender.user_id,
            entity_name=sender.display_name,
            source_message_id=message.message_id,
        )
        
        ok = db.store_memory(message.chat.id, record)
        if hasattr(ai.memory, "store_single_fact"):
            ai.memory.store_single_fact(message.chat.id, f"{sender.display_name}: {fact[:500]}", sender.display_name)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Забыть этот факт", callback_data=f"forget_fact_{message.message_id}")]
        ])
        
        await message.answer(
            "🧠 <b>Запомнила.</b> Лишнего вокруг этого сообщения в память не беру.",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    @router.callback_query(F.data.startswith("forget_fact_"))
    async def forget_fact_callback(query: CallbackQuery) -> None:
        # В aiogram 3 callback_query.message может быть разным, берем ID
        msg_id_str = query.data.replace("forget_fact_", "")
        try:
            msg_id = int(msg_id_str)
            # Удаляем по ID сообщения-источника
            deleted = db.delete_memory_by_source_id(query.message.chat.id, msg_id)
            if deleted:
                await query.answer("🧹 Факт удален из памяти!", show_alert=True)
                await query.message.edit_text("🧹 <b>Этот факт был удален из моей памяти.</b>", parse_mode="HTML")
            else:
                await query.answer("❌ Не нашла такой записи или она уже удалена.", show_alert=True)
        except Exception as e:
            print(f"Error in forget_fact_callback: {e}")
            await query.answer("❌ Произошла ошибка при удалении.")

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

    @router.message(Command("setflavor", "вкус"))
    async def set_flavor_command(message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("🍦 <b>Твой вкус</b>\n\nИспользование: <code>/setflavor &lt;вкус&gt;</code>\nПример: <code>/setflavor Шоколадный</code>", parse_mode="HTML")
            return
        sender = get_sender_data(message)
        user = db.get_or_create_user(message.chat.id, sender)
        db.update_user(user.id, {"flavor": command.args.strip()})
        await message.answer(f"✅ Теперь твой вкус: <b>{escape_html(command.args.strip())}</b>", parse_mode="HTML")

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

    def _get_dynamic_price(base_price: int, count_24h: int) -> int:
        """Рассчитывает динамическую цену в зависимости от спроса за 24 часа."""
        if count_24h < 5:
            return base_price
        elif count_24h < 15:
            return int(base_price * 1.5)
        elif count_24h < 30:
            return int(base_price * 2.5)
        else:
            return int(base_price * 4.0)

    @router.message(Command("aiimage", "картинка"))
    async def ai_image_command(message: Message, command: CommandObject) -> None:
        prompt = (command.args or "").strip()
        count_24h = db.get_recent_stats_count(message.chat.id, "aiimage_plays", hours=24)
        dynamic_price = _get_dynamic_price(ai.settings.ai_image_price, count_24h)
        
        if not prompt:
            await message.answer(
                "🖼️ <b>ИИ-картинка</b>\n\n"
                f"Цена: <b>{dynamic_price}</b> 🍪 <i>(динамическая, зависит от спроса)</i>\n"
                "Использование: <code>/aiimage описание картинки</code>\n"
                "Если ответить командой на картинку, она станет референсом позы/ракурса, а не внешности.",
                parse_mode="HTML",
            )
            return
        if message.chat.type != "private" and not db.get_chat_settings(message.chat.id).ai_enabled:
            await message.answer("🤖 ИИ-функции в этом чате сейчас выключены администратором.")
            return
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await deny_if_jailed(db, message, sender, "/aiimage"):
            return
            
        price = max(1, dynamic_price)
        if sender.reputation < price:
            await message.answer(f"❌ Для ИИ-картинки сейчас нужно <b>{price}</b> 🍪. Баланс: <b>{sender.reputation}</b> 🍪", parse_mode="HTML")
            return
            
        status_msg = await message.answer(f"🖼️ <b>Рисую...</b> Это займет 10-20 секунд.\n<i>Текущая цена: {price} 🍪</i>", parse_mode="HTML")
        
        async def _background_task():
            try:
                saved_nika_reference = _load_saved_nika_reference()
                pose_references = await _download_reference_images(message)
                reference_images = []
                if saved_nika_reference and _wants_nika_in_image(prompt):
                    reference_images.append(saved_nika_reference)
                reference_images.extend(pose_references)
                
                image_bytes = await ai.generate_image(_build_ai_image_prompt(prompt), reference_images=reference_images)
                if not image_bytes:
                    await status_msg.edit_text("❌ Не получилось сгенерировать картинку. Печеньки не списаны.")
                    return
                    
                ok, balance = await _charge_after_success(sender.user_id, message.chat.id, price)
                if not ok:
                    await status_msg.edit_text("❌ Пока картинка генерировалась, печенек уже не хватило.")
                    return
                
                db.increment_stat(sender.id, "aiimage_plays")
                await message.answer_photo(
                    BufferedInputFile(image_bytes, filename="nika_ai_image.png"),
                    caption=f"🖼️ Готово. Списано <b>{price}</b> 🍪\nБаланс: <b>{balance}</b> 🍪",
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id
                )
                await status_msg.delete()
            except Exception as e:
                print(f"Error in background aiimage: {e}")
                try:
                    await status_msg.edit_text("❌ Произошла ошибка при генерации.")
                except Exception:
                    pass

        asyncio.create_task(_background_task())

    @router.message(Command("signai", "aisign", "сигнаии"))
    async def ai_sign_command(message: Message, command: CommandObject) -> None:
        text = (command.args or "").strip()
        count_24h = db.get_recent_stats_count(message.chat.id, "signai_plays", hours=24)
        dynamic_price = _get_dynamic_price(ai.settings.ai_sign_price, count_24h)
        
        if not text:
            await message.answer(
                "✍️ <b>ИИ-сигна</b>\n\n"
                f"Цена: <b>{dynamic_price}</b> 🍪 <i>(динамическая, зависит от спроса)</i>\n"
                "Использование: <code>/signai текст на сигне</code>\n"
                "На картинке будет Ника, но сцена/поза/место каждый раз меняются.\n"
                "Внешность берется из сохраненного /setnika, а reply-картинка дает только позу/ракурс.",
                parse_mode="HTML",
            )
            return
        if message.chat.type != "private" and not db.get_chat_settings(message.chat.id).ai_enabled:
            await message.answer("🤖 ИИ-функции в этом чате сейчас выключены администратором.")
            return
        sender = db.get_or_create_user(message.chat.id, get_sender_data(message))
        if await deny_if_jailed(db, message, sender, "/signai"):
            return
            
        price = max(1, dynamic_price)
        if sender.reputation < price:
            await message.answer(f"❌ Для ИИ-сигны сейчас нужно <b>{price}</b> 🍪. Баланс: <b>{sender.reputation}</b> 🍪", parse_mode="HTML")
            return
            
        status_msg = await message.answer(f"✍️ <b>Рисую сигну...</b> Это займет 10-20 секунд.\n<i>Текущая цена: {price} 🍪</i>", parse_mode="HTML")
        
        async def _background_task():
            try:
                safe_text = text[:120]
                saved_nika_reference = _load_saved_nika_reference()
                pose_references = await _download_reference_images(message)
                reference_images = []
                if saved_nika_reference:
                    reference_images.append(saved_nika_reference)
                reference_images.extend(pose_references)
                
                image_bytes = await ai.generate_image(_build_ai_sign_prompt(safe_text), reference_images=reference_images)
                if not image_bytes:
                    await status_msg.edit_text("❌ Не получилось сгенерировать ИИ-сигну. Печеньки не списаны.")
                    return
                    
                ok, balance = await _charge_after_success(sender.user_id, message.chat.id, price)
                if not ok:
                    await status_msg.edit_text("❌ Пока сигна генерировалась, печенек уже не хватило.")
                    return
                
                db.increment_stat(sender.id, "signai_plays")
                await message.answer_photo(
                    BufferedInputFile(image_bytes, filename="nika_ai_sign.png"),
                    caption=(
                        f"✍️ ИИ-сигна готова. Списано <b>{price}</b> 🍪\n"
                        f"Баланс: <b>{balance}</b> 🍪"
                        + _reference_caption(bool(saved_nika_reference), bool(pose_references))
                    ),
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id
                )
                await status_msg.delete()
            except Exception as e:
                print(f"Error in background signai: {e}")
                try:
                    await status_msg.edit_text("❌ Произошла ошибка при генерации сигны.")
                except Exception:
                    pass

        asyncio.create_task(_background_task())

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


    @router.message(Command("forget", "забудь"))
    async def forget_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Заставлять меня забывать факты могут только администраторы.")
            return
        if not command.args:
            await message.answer(
                "🧹 <b>Забыть факт</b>\n\n"
                "Использование: <code>/forget текст факта</code>\n"
                "Я удалю из памяти все записи, содержащие этот текст.\n"
                "<i>Минимум 3 символа.</i>",
                parse_mode="HTML"
            )
            return
        query = command.args.strip()
        if len(query) < 3:
            await message.answer("❌ Запрос слишком короткий. Укажите минимум 3 символа.")
            return
        deleted_count = db.delete_memory(message.chat.id, query)
        if deleted_count > 0:
            await message.answer(
                f"🧹 <b>Готово!</b> Удалила <b>{deleted_count}</b> записей из памяти по запросу «{escape_html(query)}».",
                parse_mode="HTML"
            )
        else:
            await message.answer(
                f"🤷‍♀️ Ничего не нашла в памяти по запросу «{escape_html(query)}».",
                parse_mode="HTML"
            )

    @router.message(Command("nika_brain", "мозг"))
    async def nika_brain_command(message: Message, command: CommandObject) -> None:
        """Просмотр памяти ИИ Ники по запросу"""
        query = (command.args or "").strip()
        if not query:
            await message.answer("🧠 Использование: <code>/nika_brain запрос</code> (например: <i>/nika_brain кто любит пиццу</i>)", parse_mode="HTML")
            return

        facts = await ai.memory.get_relevant_facts(message.chat.id, query, message.from_user.first_name if message.from_user else "")
        if not facts:
            await message.answer(f"🧠 В памяти ничего не найдено по запросу: <i>{escape_html(query)}</i>", parse_mode="HTML")
            return

        await message.answer(
            f"🧠 <b>Результаты поиска в памяти по запросу «{escape_html(query)}»:</b>\n\n{escape_html(facts)}",
            parse_mode="HTML",
        )


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


    @router.message(Command("forget", "забудь"))
    async def forget_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Заставлять меня забывать факты могут только администраторы.")
            return
        if not command.args:
            await message.answer(
                "🧹 <b>Забыть факт</b>\n\n"
                "Использование: <code>/forget текст факта</code>\n"
                "Я удалю из памяти все записи, содержащие этот текст.\n"
                "<i>Минимум 3 символа.</i>",
                parse_mode="HTML"
            )
            return
        query = command.args.strip()
        if len(query) < 3:
            await message.answer("❌ Запрос слишком короткий. Укажите минимум 3 символа.")
            return
        deleted_count = db.delete_memory(message.chat.id, query)
        if deleted_count > 0:
            await message.answer(
                f"🧹 <b>Готово!</b> Удалила <b>{deleted_count}</b> записей из памяти по запросу «{escape_html(query)}».",
                parse_mode="HTML"
            )
        else:
            await message.answer(
                f"🤷‍♀️ Ничего не нашла в памяти по запросу «{escape_html(query)}».",
                parse_mode="HTML"
            )

    @router.message(Command("nika_brain", "мозг"))
    async def nika_brain_command(message: Message, command: CommandObject) -> None:
        """Просмотр памяти ИИ Ники по запросу"""
        query = (command.args or "").strip()
        if not query:
            await message.answer("🧠 Использование: <code>/nika_brain запрос</code> (например: <i>/nika_brain кто любит пиццу</i>)", parse_mode="HTML")
            return

        facts = await ai.memory.get_relevant_facts(message.chat.id, query, message.from_user.first_name if message.from_user else "")
        if not facts:
            await message.answer(f"🧠 В памяти ничего не найдено по запросу: <i>{escape_html(query)}</i>", parse_mode="HTML")
            return

        await message.answer(
            f"🧠 <b>Результаты поиска в памяти по запросу «{escape_html(query)}»:</b>\n\n{escape_html(facts)}",
            parse_mode="HTML",
        )

    @router.message(Command("backup_memory", "бэкап"))
    async def backup_memory_command(message: Message) -> None:
        """Принудительная выгрузка бэкапа векторной базы данных в Telegram"""
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Эта команда доступна только администраторам.")
            return

        if hasattr(ai.memory, "backup_service") and ai.memory.backup_service:
            success = await ai.memory.backup_service.upload_backup("💾 Ручной бэкап памяти по команде /backup_memory")
            if success:
                await message.answer("💾 <b>Успех!</b> Файл бэкапа векторной памяти выгружен в бэкап-чат.", parse_mode="HTML")
            else:
                await message.answer("❌ <b>Ошибка выгрузки:</b> Проверьте права бота в чате бэкапа.", parse_mode="HTML")
        else:
            await message.answer("ℹ️ Использование бэкапов доступно при <code>MEMORY_PROVIDER=chroma</code>.", parse_mode="HTML")

    @router.message(Command("restore_memory", "восстановить"))
    async def restore_memory_command(message: Message) -> None:
        """Восстановление векторной памяти из прикрепленного zip файла бэкапа"""
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Восстанавливать память могут только администраторы.")
            return

        doc = message.document or (message.reply_to_message.document if message.reply_to_message else None)
        if not doc or not (doc.file_name and doc.file_name.endswith(".zip")):
            await message.answer("📥 Отправь zip-файл бэкапа с подписью <code>/restore_memory</code> или ответь на файл этим сообщением.", parse_mode="HTML")
            return

        file = await message.bot.get_file(doc.file_id)
        file_bytes = await message.bot.download_file(file.file_path)

        if hasattr(ai.memory, "restore_from_zip_bytes"):
            success = await ai.memory.restore_from_zip_bytes(file_bytes.read())
            if success:
                await message.answer("✅ <b>Память Ники успешно восстановлена из бэкап-файла!</b>", parse_mode="HTML")
            else:
                await message.answer("❌ Ошибка при распаковке архива памяти.", parse_mode="HTML")
        else:
            await message.answer("ℹ️ Восстановление из архивов доступно при <code>MEMORY_PROVIDER=chroma</code>.", parse_mode="HTML")

    @router.message(Command("add_fact", "добавить_факт"))
    async def add_fact_command(message: Message, command: CommandObject) -> None:
        """Ручное добавление факта администратором"""
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Добавлять факты вручную могут только администраторы.")
            return

        text = (command.args or "").strip()
        if not text:
            await message.answer("📝 <b>Добавление факта</b>\n\nИспользование: <code>/add_fact [@username] текст факта</code>", parse_mode="HTML")
            return

        entity_name = ""
        if text.startswith("@"):
            parts = text.split(" ", 1)
            entity_name = parts[0].lstrip("@")
            text = parts[1] if len(parts) > 1 else text

        if hasattr(ai.memory, "add_single_fact"):
            success = ai.memory.add_single_fact(message.chat.id, text, entity_name=entity_name)
            if success:
                await message.answer(f"✅ <b>Факт сохранён в мозг Ники!</b>\n\n<i>{escape_html(text)}</i>", parse_mode="HTML")
            else:
                await message.answer("❌ Не удалось сохранить факт.")
        else:
            db.add_memory(message.chat.id, sender.user_id, text, "confirmed")
            await message.answer(f"✅ <b>Факт сохранён!</b>\n\n<i>{escape_html(text)}</i>", parse_mode="HTML")

    @router.message(Command("edit_fact", "изменить_факт"))
    async def edit_fact_command(message: Message, command: CommandObject) -> None:
        """Редактирование текста фактов администратором"""
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Редактировать факты могут только администраторы.")
            return

        args = (command.args or "").strip()
        if "=>" not in args:
            await message.answer(
                "✏️ <b>Замена текста в памяти</b>\n\n"
                "Использование: <code>/edit_fact старый текст => новый текст</code>\n"
                "<i>Пример: /edit_fact Любит котиков => Обожает собачек</i>",
                parse_mode="HTML"
            )
            return

        old_part, _, new_part = args.partition("=>")
        old_text = old_part.strip()
        new_text = new_part.strip()

        if hasattr(ai.memory, "edit_fact_text"):
            count = ai.memory.edit_fact_text(message.chat.id, old_text, new_text)
            if count > 0:
                await message.answer(f"✏️ <b>Успешно изменено {count} записей!</b>\n\n«{escape_html(old_text)}» ➔ «{escape_html(new_text)}»", parse_mode="HTML")
            else:
                await message.answer(f"🤷‍♀️ Записи со словом «{escape_html(old_text)}» не найдены в памяти.", parse_mode="HTML")
        else:
            await message.answer("ℹ️ Прямое редактирование текста доступно при <code>MEMORY_PROVIDER=chroma</code>.", parse_mode="HTML")

    def _build_facts_keyboard(chat_id: int, paged_data: dict[str, Any]) -> InlineKeyboardMarkup:
        keyboard_rows = []
        page = paged_data["page"]
        pages = paged_data["pages"]
        facts = paged_data["facts"]

        for idx, fact in enumerate(facts, 1):
            doc_id = fact["id"]
            short_id = doc_id[:16]
            keyboard_rows.append([
                InlineKeyboardButton(
                    text=f"❌ Удалить #{idx}",
                    callback_data=f"del_fact_{page}_{short_id}"
                )
            ])

        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"facts_page_{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"Стр {page}/{pages}", callback_data="ignore_facts"))
        if page < pages:
            nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"facts_page_{page + 1}"))
        
    def _build_facts_keyboard(chat_id: int, paged_data: dict[str, Any]) -> InlineKeyboardMarkup:
        keyboard_rows = []
        page = paged_data["page"]
        pages = paged_data["pages"]
        facts = paged_data["facts"]
        query = paged_data.get("query", "")
        q_param = f":{query}" if query else ""

        for idx, fact in enumerate(facts, 1):
            doc_id = fact["id"]
            short_id = doc_id[:16]
            keyboard_rows.append([
                InlineKeyboardButton(
                    text=f"✏️ Изменить #{idx}",
                    callback_data=f"edit_fact_{page}_{short_id}"
                ),
                InlineKeyboardButton(
                    text=f"❌ Удалить #{idx}",
                    callback_data=f"del_fact_{page}_{short_id}"
                )
            ])

        # Быстрый переход в самое начало и в самый конец
        fast_nav = []
        if page > 1:
            fast_nav.append(InlineKeyboardButton(text="⏮ В начало", callback_data=f"facts_page_1{q_param}"))
        if page < pages:
            fast_nav.append(InlineKeyboardButton(text="В конец ⏭", callback_data=f"facts_page_{pages}{q_param}"))
        if fast_nav:
            keyboard_rows.append(fast_nav)

        # Основная навигация: [ ◀️ Назад ] [ 🔢 Стр X/Y ] [ Вперёд ▶️ ]
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"facts_page_{page - 1}{q_param}"))
        nav_row.append(InlineKeyboardButton(text=f"🔢 Стр {page}/{pages}", callback_data=f"facts_goto_{page}"))
        if page < pages:
            nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"facts_page_{page + 1}{q_param}"))
        
        keyboard_rows.append(nav_row)
        return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    @router.message(Command("all_facts", "факты_меню"))
    async def all_facts_command(message: Message, command: CommandObject) -> None:
        """Интерактивное меню просмотра и удаления всех фактов с фильтрацией по участникам/словам"""
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Интерактивный менеджер фактов доступен только администраторам.")
            return

        if not hasattr(ai.memory, "get_all_facts_paged"):
            await message.answer("ℹ️ Просмотр списка фактов доступен при <code>MEMORY_PROVIDER=chroma</code>.", parse_mode="HTML")
            return

        target_page = 1
        query = ""
        raw_args = (command.args or "").strip()
        if raw_args.isdigit():
            target_page = int(raw_args)
        else:
            query = raw_args

        paged_data = ai.memory.get_all_facts_paged(message.chat.id, page=target_page, page_size=5, query=query)
        if not paged_data["facts"]:
            q_info = f" по запросу «<b>{escape_html(query)}</b>»" if query else ""
            await message.answer(f"🧠 В памяти пока нет сохранённых фактов{q_info}.", parse_mode="HTML")
            return

        filter_title = f" (фильтр: «<b>{escape_html(query)}</b>»)" if query else ""
        lines = [f"📚 <b>Интерактивная база знаний Ники{filter_title} (Найдено: {paged_data['total']})</b>\n"]
        for idx, item in enumerate(paged_data["facts"], 1):
            entity = f" [@{item['entity_name']}]" if item.get('entity_name') else ""
            lines.append(f"<b>#{idx}</b>{escape_html(entity)}: <i>{escape_html(item['text'][:120])}</i>")

        reply_markup = _build_facts_keyboard(message.chat.id, paged_data)
        await message.answer("\n\n".join(lines), reply_markup=reply_markup, parse_mode="HTML")

    @router.callback_query(F.data.startswith("facts_goto_"))
    async def facts_goto_callback(callback: CallbackQuery) -> None:
        await callback.answer(
            "💡 Для фильтрации или перехода укажите параметр:\n/all_facts @SCTemi или /all_facts 15",
            show_alert=True
        )

    @router.callback_query(F.data.startswith("facts_page_"))
    async def facts_page_callback(callback: CallbackQuery) -> None:
        payload = callback.data.replace("facts_page_", "")
        page_str, _, query = payload.partition(":")
        page = int(page_str) if page_str.isdigit() else 1

        paged_data = ai.memory.get_all_facts_paged(callback.message.chat.id, page=page, page_size=5, query=query)
        filter_title = f" (фильтр: «<b>{escape_html(query)}</b>»)" if query else ""
        lines = [f"📚 <b>Интерактивная база знаний Ники{filter_title} (Найдено: {paged_data['total']})</b>\n"]
        for idx, item in enumerate(paged_data["facts"], 1):
            entity = f" [@{item['entity_name']}]" if item.get('entity_name') else ""
            lines.append(f"<b>#{idx}</b>{escape_html(entity)}: <i>{escape_html(item['text'][:120])}</i>")

        reply_markup = _build_facts_keyboard(callback.message.chat.id, paged_data)
        await callback.message.edit_text("\n\n".join(lines), reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer()

    @router.callback_query(F.data.startswith("edit_fact_"))
    async def edit_fact_callback(callback: CallbackQuery) -> None:
        sender_id = callback.from_user.id
        if not await is_admin(callback.bot, callback.message.chat.id, sender_id):
            await callback.answer("❌ Редактировать факты могут только администраторы.", show_alert=True)
            return

        parts = callback.data.split("_")
        page = int(parts[2])
        short_id = parts[3]

        all_data = ai.memory.get_all_facts_paged(callback.message.chat.id, page=1, page_size=1000)
        target_item = None
        for item in all_data["facts"]:
            if item["id"].startswith(short_id):
                target_item = item
                break

        if not target_item:
            await callback.answer("❌ Не удалось найти этот факт.", show_alert=True)
            return

        await callback.message.answer(
            f"✏️ <b>Редактирование факта (ID: <code>{target_item['id']}</code>)</b>\n\n"
            f"Текущий текст:\n<i>{escape_html(target_item['text'])}</i>\n\n"
            "<b>Ответьте (Reply) на это сообщение новым текстом для этого факта.</b>",
            parse_mode="HTML"
        )
        await callback.answer()

    @router.message(F.reply_to_message & F.reply_to_message.text.contains("Редактирование факта (ID:"))
    async def handle_edit_fact_reply(message: Message) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("❌ Редактировать факты могут только администраторы.")
            return

        reply_text = message.reply_to_message.text or ""
        match = re.search(r"\(ID:\s*<code>([^<]+)</code>\)", reply_text)
        if not match:
            return

        doc_id = match.group(1).strip()
        new_text = (message.text or "").strip()
        if not new_text:
            await message.answer("❌ Новое содержание факта не может быть пустым.")
            return

        if hasattr(ai.memory, "update_fact_text_by_id") and ai.memory.update_fact_text_by_id(message.chat.id, doc_id, new_text):
            await message.answer(f"✅ <b>Факт успешно обновлён!</b>\n\nНовый текст:\n<i>{escape_html(new_text)}</i>", parse_mode="HTML")
        else:
            await message.answer("❌ Не удалось обновить факт.")

    @router.callback_query(F.data.startswith("del_fact_"))
    async def delete_fact_callback(callback: CallbackQuery) -> None:
        sender_id = callback.from_user.id
        if not await is_admin(callback.bot, callback.message.chat.id, sender_id):
            await callback.answer("❌ Удалять факты могут только администраторы.", show_alert=True)
            return

        parts = callback.data.split("_")
        page = int(parts[2])
        short_id = parts[3]

        all_data = ai.memory.get_all_facts_paged(callback.message.chat.id, page=1, page_size=1000)
        target_id = None
        for item in all_data["facts"]:
            if item["id"].startswith(short_id):
                target_id = item["id"]
                break

        if target_id and ai.memory.delete_fact_by_id(callback.message.chat.id, target_id):
            await callback.answer("✅ Факт успешно удалён из памяти!", show_alert=True)
        else:
            await callback.answer("❌ Не удалось найти или удалить факт.", show_alert=True)

        paged_data = ai.memory.get_all_facts_paged(callback.message.chat.id, page=page, page_size=5)
        if not paged_data["facts"]:
            await callback.message.edit_text("🧠 В памяти больше нет сохранённых фактов.")
            return

        lines = [f"📚 <b>Интерактивная база знаний Ники (Всего: {paged_data['total']})</b>\n"]
        for idx, item in enumerate(paged_data["facts"], 1):
            entity = f" [@{item['entity_name']}]" if item.get('entity_name') else ""
            lines.append(f"<b>#{idx}</b>{escape_html(entity)}: <i>{escape_html(item['text'][:120])}</i>")

        reply_markup = _build_facts_keyboard(callback.message.chat.id, paged_data)
        await callback.message.edit_text("\n\n".join(lines), reply_markup=reply_markup, parse_mode="HTML")

    return router




