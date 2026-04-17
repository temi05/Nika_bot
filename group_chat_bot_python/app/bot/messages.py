from __future__ import annotations

import asyncio
import re
from datetime import datetime

from aiogram import Bot, Router
from aiogram.types import BufferedInputFile, ChatPermissions, Message
from aiogram.utils.chat_action import ChatActionSender

from app.config import Settings
from app.models import VerificationChallenge
from app.services.ai_service import AIService
from app.services.supabase_db import SupabaseDB
from app.utils import birthday_age_text, build_captcha_image, escape_html, generate_captcha_code, get_sender_data


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

        text = message.text or message.caption or ""
        is_media = bool(message.photo or message.sticker or message.video or message.document)
        db.store_message_author(message.chat.id, message.message_id, sender.user_id)

        if text.startswith("/"):
            return

        user = db.get_or_create_user(message.chat.id, sender)
        updated_user, level_up = db.apply_message_xp(user)
        if level_up and updated_user:
            await message.answer(
                f"🎉 <b>{escape_html(sender.display_name)}</b> апнул <b>{updated_user.level}</b> уровень!",
                parse_mode="HTML",
            )

        ai.remember_message(message.chat.id, sender, text or "[media]")
        try:
            await ai.flush_passive_memory(message.chat.id)
        except Exception as exc:
            print(f"[AI:flush_memory_error] chat_id={message.chat.id} error={exc}")

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

        me = await bot.get_me()
        
        # Более гибкая проверка имени: реагируем и на полное имя, и на короткое "Ника"
        is_mentioned = _message_mentions_bot(text, settings.bot_name, me.username)
            
        is_reply_to_bot = False
        if message.reply_to_message and message.reply_to_message.from_user:
            is_reply_to_bot = (message.reply_to_message.from_user.id == me.id)

        # ЛОГИ ДЛЯ ОТЛАДКИ (появятся в Render)
        print(f"🔍 [DEBUG] Msg from {sender.display_name}: mentioned={is_mentioned}, reply_to_bot={is_reply_to_bot}")
        if is_reply_to_bot:
            print(f"   └ Bot ID: {me.id}, Reply to User ID: {message.reply_to_message.from_user.id}")

        if text or is_media:
            caller_is_admin = await _message_sender_is_admin(bot, message, sender)
            is_private_chat = message.chat.type == "private"
            should_reply = is_private_chat or is_reply_to_bot or is_mentioned
            reply = None
            if should_reply:
                async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
                    reply = await ai.generate_reply(
                        message.chat.id,
                        sender,
                        text or "[media]",
                        is_reply_to_bot,
                        is_mentioned,
                        caller_is_admin,
                        is_private_chat=is_private_chat,
                    )
            if reply:
                await message.reply(reply)

    return router


def _word_in_text(text: str, word: str) -> bool:
    return bool(re.search(rf"(^|\W){re.escape(word)}($|\W)", text, flags=re.IGNORECASE))


def _reputation_delta(text: str) -> int:
    positive = {"+", "спасибо", "👍", "спс"}
    negative = {"-", "👎", "фу"}
    if text in positive:
        return 1
    if text in negative:
        return -1
    return 0


async def _user_is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if not user_id or user_id <= 0:
        return False
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
