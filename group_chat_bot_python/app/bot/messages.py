from __future__ import annotations

import asyncio
import base64
import re
from io import BytesIO
from datetime import datetime

from aiogram import Bot, Router
from aiogram.types import BufferedInputFile, ChatPermissions, Message
from aiogram.utils.chat_action import ChatActionSender

from app.config import Settings
from app.models import Sender, VerificationChallenge
from app.services.ai_service import AIService
from app.services.supabase_db import SupabaseDB
from app.utils import birthday_age_text, build_captcha_image, escape_html, generate_captcha_code, get_sender_data


def _looks_like_memory_signal(text: str) -> bool:
    clean = re.sub(r"\s+", " ", text.strip().lower())
    if len(clean) < 10 or clean.startswith("/"):
        return False
    if clean.count("http://") or clean.count("https://"):
        return False

    strong_markers = (
        "запомни",
        "запиши",
        "сохрани",
        "не забудь",
        "меня зовут",
        "мой ник",
        "мой день рождения",
        "мне нравится",
        "я люблю",
        "я не люблю",
        "я ненавижу",
        "у меня",
        "я из ",
        "я живу",
        "я учусь",
        "я работаю",
        "remember",
        "my name is",
        "i like",
        "i don't like",
    )
    if any(marker in clean for marker in strong_markers):
        return True

    if re.search(r"\bмне\s+\d{1,2}\s+(год|года|лет)\b", clean):
        return True
    if re.search(r"\b(мой|моя|мои)\s+.{3,40}\s*[-—:]\s*.{2,80}", clean):
        return True
    return False


def build_messages_router(bot: Bot, settings: Settings, db: SupabaseDB, ai: AIService) -> Router:
    router = Router(name="messages")

    async def expire_verification(challenge: VerificationChallenge) -> None:
        await asyncio.sleep(challenge.timeout_seconds)
        still_pending = db.get_verification(challenge.user_id)
        if not still_pending or still_pending.code != challenge.code:
            return
        db.pop_verification(challenge.user_id)
        try:
            await bot.ban_chat_member(
                challenge.chat_id,
                challenge.user_id,
                until_date=int(datetime.now().timestamp()) + 60,
            )
        except Exception:
            return
        try:
            await bot.delete_message(challenge.chat_id, challenge.prompt_message_id)
        except Exception:
            pass

    @router.message()
    async def handle_message(message: Message) -> None:
        sender = get_sender_data(message)

        if message.new_chat_members:
            me = await bot.get_me()
            inviter = get_sender_data(message)
            for member in message.new_chat_members:
                if member.is_bot:
                    if member.id != me.id:
                        try:
                            await bot.ban_chat_member(message.chat.id, member.id)
                        except Exception:
                            pass
                        await message.answer(
                            f"🚨 Обнаружен бот. Пригласил: <b>{escape_html(inviter.display_name)}</b>",
                            parse_mode="HTML",
                        )
                    continue

                try:
                    await bot.restrict_chat_member(
                        message.chat.id,
                        member.id,
                        permissions=ChatPermissions(
                            can_send_messages=True,
                            can_send_audios=False,
                            can_send_documents=False,
                            can_send_photos=False,
                            can_send_videos=False,
                            can_send_video_notes=False,
                            can_send_voice_notes=False,
                            can_send_polls=False,
                            can_send_other_messages=False,
                            can_add_web_page_previews=False,
                            can_change_info=False,
                            can_invite_users=False,
                            can_pin_messages=False,
                            can_manage_topics=False,
                        ),
                    )
                except Exception:
                    continue

                code = generate_captcha_code()
                image = build_captcha_image(code)
                sent = await bot.send_photo(
                    message.chat.id,
                    BufferedInputFile(image.getvalue(), filename=f"captcha_{member.id}.png"),
                    caption="👋 Привет! Напиши цифры с картинки за 2 минуты.",
                )
                challenge = VerificationChallenge(
                    chat_id=message.chat.id,
                    user_id=member.id,
                    code=code,
                    prompt_message_id=sent.message_id,
                    created_at=datetime.utcnow(),
                )
                db.set_verification(challenge)
                asyncio.create_task(expire_verification(challenge))
            return

        if sender.is_bot:
            return

        pending = db.get_verification(sender.user_id)
        if pending:
            if (message.text or "").strip() == pending.code and pending.chat_id == message.chat.id:
                db.pop_verification(sender.user_id)
                try:
                    await bot.restrict_chat_member(
                        message.chat.id,
                        sender.user_id,
                        permissions=ChatPermissions(
                            can_send_messages=True,
                            can_send_audios=True,
                            can_send_documents=True,
                            can_send_photos=True,
                            can_send_videos=True,
                            can_send_video_notes=True,
                            can_send_voice_notes=True,
                            can_send_polls=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True,
                            can_change_info=False,
                            can_invite_users=True,
                            can_pin_messages=False,
                            can_manage_topics=False,
                        ),
                    )
                except Exception:
                    pass
                try:
                    await bot.delete_message(message.chat.id, pending.prompt_message_id)
                except Exception:
                    pass
                try:
                    await message.delete()
                except Exception:
                    pass
                await message.answer(
                    f"✅ <b>{escape_html(sender.display_name)}</b> прошёл проверку!",
                    parse_mode="HTML",
                )
            else:
                try:
                    await message.delete()
                except Exception:
                    pass
            return

        if message.pinned_message or message.left_chat_member or message.new_chat_title:
            return

        raw_text = message.text or message.caption or ""
        ai_input_text = _build_ai_input_text(message, raw_text)
        memory_input_text = ai_input_text
        if ai_input_text:
            reply_context = _build_reply_context(message.reply_to_message)
            if reply_context:
                ai_input_text = f"{reply_context}\n\n{ai_input_text}"
        text = raw_text.strip()
        db.store_message_context(
            message.chat.id,
            message.message_id,
            sender,
            _build_message_log_text(message, raw_text),
            message_type=_message_log_type(message),
            reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
        )

        if text.startswith("/"):
            return

        user = db.get_or_create_user(message.chat.id, sender)
        db.increment_stat(user.id, "total_messages") # Увеличиваем счетчик сообщений
        
        should_grant_xp = _should_grant_xp(message, sender)
        if should_grant_xp:
            updated_user, level_up = db.apply_message_xp(user)
            if level_up and updated_user:
                await message.answer(
                    f"🎉 <b>{escape_html(sender.display_name)}</b> апнул <b>{updated_user.level}</b> уровень!",
                    parse_mode="HTML",
                )

        if text:
            settings_row = db.get_chat_settings(message.chat.id)
            bad_words = db.get_bad_words(message.chat.id)
            lowered = text.lower()
            found_bad_word = any(word and _word_in_text(lowered, word) for word in bad_words)
            promo_link = "t.me/" in lowered or "telegram.me/" in lowered

            sender_is_admin = await _message_sender_is_admin(bot, message, sender)
            if found_bad_word or (promo_link and settings_row.link_filter_enabled and not sender_is_admin):
                try:
                    await message.delete()
                except Exception:
                    pass
                updated = db.apply_warn(user)
                warns_now = updated.warns if updated else user.warns + 1
                reason = "Ссылки t.me запрещены." if promo_link and settings_row.link_filter_enabled else "Нарушение правил чата."
                await message.answer(f"{sender.display_name}, {reason} Предупреждение {warns_now}/{settings.warn_limit}.")
                return

            if message.reply_to_message:
                change = _reputation_delta(lowered)
                if change != 0:
                    reply_sender = get_sender_data(message.reply_to_message)
                    if reply_sender.user_id != sender.user_id and db.can_adjust_reputation(sender.user_id, reply_sender.user_id):
                        receiver = db.get_or_create_user(message.chat.id, reply_sender)
                        db.update_user(receiver.id, {"reputation": receiver.reputation + change})
                        total = receiver.reputation + change
                        if change > 0:
                            await message.answer(
                                f"🌟 <b>{escape_html(sender.display_name)}</b> передал печеньку "
                                f"<b>{escape_html(reply_sender.display_name)}</b>.\n"
                                f"└ Теперь у него <code>{total} 🍪</code>",
                                parse_mode="HTML",
                            )
                        else:
                            await message.answer(
                                f"📉 <b>{escape_html(sender.display_name)}</b> отнял печеньку у "
                                f"<b>{escape_html(reply_sender.display_name)}</b>.\n"
                                f"└ Осталось <code>{total} 🍪</code>",
                                parse_mode="HTML",
                            )

        today_key = datetime.now().strftime("%Y-%m-%d")
        if db.last_birthday_check.get(message.chat.id) != today_key:
            birthdays = db.get_birthdays_today(message.chat.id)
            if birthdays:
                lines = ["🎂 <b>Сегодня день рождения!</b>", ""]
                for birthday_user in birthdays:
                    lines.append(
                        f"🌟 Поздравляем <b>{escape_html(birthday_user.display_name)}</b>"
                        f"{birthday_age_text(birthday_user.birthday)}! 🥳"
                    )
                lines.append("")
                lines.append("<i>Желаем море печенек и высокого уровня во всём!</i>")
                await message.answer("\n".join(lines), parse_mode="HTML")
            db.last_birthday_check[message.chat.id] = today_key
        # Обновляем время последнего сообщения теперь через middleware в web.py

        bot_username = settings.bot_username
        bot_id = None
        try:
            me = await bot.get_me()
            bot_id = me.id
            bot_username = me.username or bot_username
        except Exception as exc:
            print(f"[BOT:get_me_error] chat_id={message.chat.id} error={exc}")

        # Реагируем только на полное имя бота и @username
        is_mentioned = _message_mentions_bot(text, settings.bot_name, bot_username)

        is_reply_to_bot = False
        if message.reply_to_message and message.reply_to_message.from_user:
            if bot_id is not None:
                is_reply_to_bot = message.reply_to_message.from_user.id == bot_id
            elif bot_username:
                reply_username = (message.reply_to_message.from_user.username or "").lower()
                is_reply_to_bot = reply_username == bot_username.lower()

        # ЛОГИ ДЛЯ ОТЛАДКИ (появятся в Render)
        print(f"🔍 [DEBUG] Msg: '{text[:50]}', mentioned={is_mentioned}, reply_to_bot={is_reply_to_bot}")
        
        if ai_input_text:
            caller_is_admin = await _message_sender_is_admin(bot, message, sender)
            is_private_chat = message.chat.type == "private"
            chat_settings = db.get_chat_settings(message.chat.id)
            should_reply = is_private_chat or is_reply_to_bot or is_mentioned
            if not is_private_chat and not chat_settings.ai_enabled:
                should_reply = False
            
            print(f"🔍 [DEBUG] should_reply={should_reply}, is_private={is_private_chat}, ai_enabled={chat_settings.ai_enabled}")
            memory_signal = _looks_like_memory_signal(text)
            should_capture_memory = (
                settings.memory_capture_all_messages
                or should_reply
                or memory_signal
            )
            if should_capture_memory:
                ai.remember_message(message.chat.id, sender, memory_input_text)
                try:
                    await ai.flush_passive_memory(message.chat.id, force=memory_signal)
                except Exception as exc:
                    print(f"[AI:flush_memory_error] chat_id={message.chat.id} error={exc}")
            reply = None
            if should_reply:
                print(f"🤖 [AI] Starting generation for chat {message.chat.id}...")
                image_data_urls = []
                if settings.ai_vision_enabled:
                    image_data_urls = await _build_ai_image_inputs(
                        bot,
                        message,
                        max_images=max(0, settings.ai_vision_max_images),
                        max_bytes=max(0, settings.ai_vision_max_bytes),
                    )
                async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
                    try:
                        reply = await asyncio.wait_for(
                            ai.generate_reply(
                                message.chat.id,
                                sender,
                                ai_input_text,
                                is_reply_to_bot,
                                is_mentioned,
                                caller_is_admin,
                                is_private_chat=is_private_chat,
                                image_data_urls=image_data_urls,
                            ),
                            timeout=25.0
                        )
                    except asyncio.TimeoutError:
                        print(f"❌ [AI] Timeout in chat {message.chat.id}")
                        reply = "Прости, я слишком долго думала и запуталась в своих мыслях. Попробуй ещё раз!"
                    except Exception as e:
                        print(f"❌ [AI] Crash in chat {message.chat.id}: {e}")
                        reply = "Ой, что-то пошло не так в моих схемах. Давай попробуем позже."
                print(f"🤖 [AI] Generation finished. Reply length: {len(reply) if reply else 0}")
            
            if should_reply and reply is None:
                print("⚠️ [AI] Generation failed (None), using fallback.")
                reply = "Я что-то задумалась и потеряла нить разговора... Можешь повторить?"

            if reply is not None:
                # Если ответ пустой (но не None), значит ИИ отработал только инструментами (стикер, реакция и т.д.)
                # Это нормальное поведение.
                if reply.strip() == "":
                    print("✉️ [AI] Tool-only action performed, skipping text reply.")
                else:
                    print(f"✉️ [AI] Sending reply to chat {message.chat.id}...")
                    bot_sender = Sender(
                        user_id=bot_id or 0,
                        first_name=settings.bot_name,
                        username=bot_username,
                        is_bot=True,
                    )
                    await _send_ai_reply(bot, message, reply, db=db, bot_sender=bot_sender)

    return router


async def _send_ai_reply(bot: Bot, message: Message, reply: str, *, db: SupabaseDB, bot_sender: Sender) -> None:
    text = (reply or "").strip()
    if not text:
        return

    chunks = _split_telegram_text(text)
    for index, chunk in enumerate(chunks):
        try:
            if index == 0:
                sent = await message.reply(chunk)
            else:
                sent = await bot.send_message(message.chat.id, chunk)
            db.store_message_context(
                message.chat.id,
                sent.message_id,
                bot_sender,
                chunk,
                message_type="bot_reply",
                reply_to_message_id=message.message_id if index == 0 else None,
            )
        except Exception as exc:
            print(
                "[AI:telegram_send_error] "
                f"chat_id={message.chat.id} message_id={message.message_id} "
                f"chunk={index + 1}/{len(chunks)} error={exc}"
            )
            try:
                sent = await bot.send_message(message.chat.id, chunk)
                db.store_message_context(
                    message.chat.id,
                    sent.message_id,
                    bot_sender,
                    chunk,
                    message_type="bot_reply",
                    reply_to_message_id=None,
                )
            except Exception as fallback_exc:
                print(
                    "[AI:telegram_send_fallback_error] "
                    f"chat_id={message.chat.id} message_id={message.message_id} "
                    f"chunk={index + 1}/{len(chunks)} error={fallback_exc}"
                )


def _split_telegram_text(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return [chunk for chunk in chunks if chunk]


def _word_in_text(text: str, word: str) -> bool:
    return bool(re.search(rf"(^|\W){re.escape(word)}($|\W)", text, flags=re.IGNORECASE))


def _reputation_delta(text: str) -> int:
    normalized = re.sub(r"\s+", " ", (text or "").strip()).casefold()
    if not normalized:
        return 0

    # Если есть отрицание перед ключевым словом, отменяем действие
    negations = {"не", "ни разу", "вовсе не", "совсем не", "никак не"}
    
    positive_exact = {"+", "+1", "++", "👍", "🤝"}
    negative_exact = {"-", "-1", "👎"}
    
    positive_markers = {
        "спасибо", "спс", "красава", "хорош", "база", "топ", 
        "сильно", "годно", "респект", "красавчик", "лучший", "умница"
    }
    negative_markers = {"фу", "кринж", "минус", "плохо", "отстой", "бесит"}

    # Проверка точных совпадений (символы)
    if normalized in positive_exact: return 1
    if normalized in negative_exact: return -1

    # Проверка слов с учетом отрицаний
    words = normalized.split()
    for i, word in enumerate(words):
        clean_word = word.strip(".,!?")
        
        # Проверяем, нет ли перед словом отрицания
        has_negation = i > 0 and words[i-1] in negations
        
        if clean_word in positive_markers:
            return 0 if has_negation else 1
        if clean_word in negative_markers:
            return 0 if has_negation else -1
            
    return 0


async def _user_is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if not user_id or user_id <= 0:
        return False
    # Superadmin hardcode
    if user_id == 861713427:
        return True
        
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in {"creator", "administrator"}


async def _message_sender_is_admin(bot: Bot, message: Message, sender) -> bool:
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True
    return await _user_is_admin(bot, message.chat.id, sender.user_id)


def _message_mentions_bot(text: str, bot_name: str, username: str | None) -> bool:
    if not text:
        return False

    normalized = text.casefold()

    if username and f"@{username.casefold()}" in normalized:
        return True

    name = bot_name.strip().casefold()
    if not name:
        return False

    return bool(re.search(rf"(^|\W){re.escape(name)}($|\W)", normalized, flags=re.IGNORECASE))


def _should_grant_xp(message: Message, sender) -> bool:
    if sender.is_bot:
        return False
    if message.sender_chat:
        # Сообщения "от имени канала/анон-админа" не привязываем к XP,
        # чтобы опыт не улетал в не-человеческие профили.
        return False

    has_meaningful_content = bool(
        (message.text or "").strip()
        or (message.caption or "").strip()
        or message.sticker
        or message.photo
        or message.video
        or message.animation
        or message.voice
        or message.video_note
        or message.audio
        or message.document
    )
    return has_meaningful_content


def _message_log_type(message: Message) -> str:
    return _describe_message_media(message).split(" ", 1)[0] or "text"


def _build_message_log_text(message: Message, raw_text: str) -> str:
    text = (raw_text or "").strip()
    media = _describe_message_media(message)
    if not media:
        return text
    if text:
        return f"[{media}] {text}"
    return f"[{media}]"


def _build_ai_input_text(message: Message, raw_text: str) -> str:
    text = (raw_text or "").strip()
    media = _describe_message_media(message)
    sender = get_sender_data(message)

    is_forwarded = getattr(message, "forward_date", None) is not None
    forward_note = ' forwarded="true"' if is_forwarded else ""

    if media:
        return f'<msg id="{message.message_id}" author="{sender.display_name}" user_id="{sender.user_id}" type="{media}"{forward_note}>\n{text}\n</msg>'

    if not text:
        return ""

    return f'<msg id="{message.message_id}" author="{sender.display_name}" user_id="{sender.user_id}" type="text"{forward_note}>\n{text}\n</msg>'


def _build_reply_context(reply: Message | None) -> str:
    if not reply:
        return ""

    reply_sender = get_sender_data(reply)
    reply_text = (reply.text or reply.caption or "").strip()
    reply_media = _describe_message_media(reply)
    media_type = reply_media if reply_media else "text"

    return f'<reply_target id="{reply.message_id}" author="{reply_sender.display_name}" user_id="{reply_sender.user_id}" type="{media_type}">\n{reply_text[:700]}\n</reply_target>'


def _describe_message_media(message: Message) -> str:
    if message.sticker:
        details = ["sticker"]
        if message.sticker.emoji:
            details.append(f"emoji={message.sticker.emoji}")
        if message.sticker.set_name:
            details.append(f"set={message.sticker.set_name}")
        if message.sticker.is_animated:
            details.append("animated=true")
        if message.sticker.is_video:
            details.append("video=true")
        return " ".join(details)
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.animation:
        return "animation"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.audio:
        return "audio"
    if message.document:
        file_name = (message.document.file_name or "").strip()
        return f"document name={file_name}" if file_name else "document"
    return ""


async def _build_ai_image_inputs(bot: Bot, message: Message, *, max_images: int, max_bytes: int) -> list[str]:
    if max_images <= 0 or max_bytes <= 0:
        return []

    image_specs: list[tuple[str, str]] = []
    current = _image_file_spec(message)
    if current:
        image_specs.append(current)
    if message.reply_to_message:
        replied = _image_file_spec(message.reply_to_message)
        if replied:
            image_specs.append(replied)

    data_urls: list[str] = []
    for file_id, mime_type in image_specs[:max_images]:
        data_url = await _download_telegram_image_as_data_url(bot, file_id, mime_type, max_bytes=max_bytes)
        if data_url:
            data_urls.append(data_url)
    return data_urls


def _image_file_spec(message: Message) -> tuple[str, str] | None:
    if message.photo:
        return message.photo[-1].file_id, "image/jpeg"

    if message.sticker:
        if not message.sticker.is_animated and not message.sticker.is_video:
            return message.sticker.file_id, "image/webp"
        thumbnail = getattr(message.sticker, "thumbnail", None)
        if thumbnail:
            return thumbnail.file_id, "image/jpeg"

    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id, message.document.mime_type or "image/jpeg"

    return None


async def _download_telegram_image_as_data_url(bot: Bot, file_id: str, mime_type: str, *, max_bytes: int) -> str | None:
    try:
        file_info = await bot.get_file(file_id)
        if file_info.file_size and file_info.file_size > max_bytes:
            print(f"[AI:vision_skip] reason=file_too_large size={file_info.file_size}")
            return None

        buffer = BytesIO()
        await bot.download_file(file_info.file_path, destination=buffer)
        payload = buffer.getvalue()
        if not payload or len(payload) > max_bytes:
            print(f"[AI:vision_skip] reason=download_too_large size={len(payload)}")
            return None

        encoded = base64.b64encode(payload).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"
    except Exception as exc:
        print(f"[AI:vision_download_error] error={exc}")
        return None
