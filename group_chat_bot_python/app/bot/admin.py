from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message

from app.config import get_superadmin_ids
from app.models import Sender
from app.services.supabase_db import SupabaseDB
from app.utils import escape_html, get_sender_data

_DURATION_RE = re.compile(r"^(\d+)\s*([smhdwсмчдн])$", re.IGNORECASE)
_ADMIN_CACHE: dict[tuple[Any, ...], tuple[bool, float]] = {}
_ADMIN_CACHE_TTL = 60.0


def invalidate_admin_cache(chat_id: int | None = None, user_id: int | None = None) -> None:
    if chat_id is None and user_id is None:
        _ADMIN_CACHE.clear()
        return
    for key in list(_ADMIN_CACHE):
        if (chat_id is None or key[1] == chat_id) and (user_id is None or key[2] == user_id):
            _ADMIN_CACHE.pop(key, None)


def make_captcha_gate_middleware(db: SupabaseDB):
    async def _pending_captcha_gate(handler, event: Message, data: dict[str, Any]) -> Any:
        if (
            getattr(event, "from_user", None)
            and getattr(event, "chat", None)
            and not getattr(event, "new_chat_members", None)
        ):
            pending = db.get_verification(event.from_user.id, chat_id=event.chat.id)
            if pending and pending.chat_id == event.chat.id:
                txt = (getattr(event, "text", None) or "").strip()
                if txt != pending.code:
                    try:
                        await event.delete()
                    except Exception:
                        pass
                    return None
        return await handler(event, data)

    return _pending_captcha_gate


def parse_duration(arg: str | None) -> timedelta | None:
    """Парсит строку длительности (например: 10m, 2h, 1d, 30м, 2ч, 1д)."""
    if not arg:
        return None
    match = _DURATION_RE.match(arg.strip())
    if not match:
        return None
    amount = int(match.group(1))
    if amount <= 0:
        return None
    unit = match.group(2).lower()
    if unit in {"s", "с"}:
        return timedelta(seconds=max(30, min(amount, 365 * 86400)))
    if unit in {"m", "м"}:
        return timedelta(minutes=min(amount, 365 * 1440))
    if unit in {"h", "ч"}:
        return timedelta(hours=min(amount, 365 * 24))
    if unit in {"d", "д"}:
        return timedelta(days=min(amount, 365))
    if unit in {"w", "н"}:
        return timedelta(weeks=min(amount, 52))
    return None


def _format_duration(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds % 86400 == 0 and total_seconds >= 86400:
        return f"{total_seconds // 86400} д."
    if total_seconds % 3600 == 0 and total_seconds >= 3600:
        return f"{total_seconds // 3600} ч."
    if total_seconds >= 60:
        return f"{total_seconds // 60} мин."
    return f"{total_seconds} сек."


def _resolve_target_and_tail(
    message: Message,
    db: SupabaseDB,
    raw_args: str | None,
) -> tuple[int | None, str, list[str]]:
    tokens = (raw_args or "").strip().split()
    if message.reply_to_message:
        reply_sender = get_sender_data(message.reply_to_message)
        return reply_sender.user_id, reply_sender.display_name, tokens

    if not tokens:
        return None, "пользователя", []

    first = tokens[0]
    tail = tokens[1:]
    if first.lstrip("-").isdigit():
        target_id = int(first)
        user = db.get_user_by_platform_id(message.chat.id, target_id)
        return target_id, (user.display_name if user else f"ID {target_id}"), tail

    found = db.search_user(message.chat.id, first)
    if found:
        return found.user_id, found.display_name, tail
    return None, "пользователя", tail


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if not user_id or user_id <= 0:
        return False
    if user_id in get_superadmin_ids():
        return True
    gm = getattr(bot, "get_chat_member", None)
    cache_key = (
        id(bot),
        chat_id,
        user_id,
        id(getattr(gm, "return_value", None)),
        id(getattr(gm, "side_effect", None)),
    )
    now = time.monotonic()
    cached = _ADMIN_CACHE.get(cache_key)
    if cached is not None and (now - cached[1]) < _ADMIN_CACHE_TTL:
        return cached[0]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        result = member.status in {"creator", "administrator"}
        _ADMIN_CACHE[cache_key] = (result, now)
        return result
    except Exception:
        return False


def build_admin_router(bot: Bot, db: SupabaseDB) -> Router:
    router = Router(name="admin")
    router.message.middleware(make_captcha_gate_middleware(db))

    @router.message(Command("ban"))
    async def ban_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return

        target_id, target_name, tail = _resolve_target_and_tail(message, db, command.args)
        if not target_id:
            await message.answer("Используй /ban в реплае или укажи @username / ID [срок] [причина].")
            return
        if await is_admin(bot, message.chat.id, target_id):
            await message.answer("Этого пользователя банить нельзя.")
            return

        duration = parse_duration(tail[0]) if tail else None
        reason_tokens = tail[1:] if (duration and tail) else tail
        reason = " ".join(reason_tokens).strip()
        until_dt = datetime.now(timezone.utc) + duration if duration else None

        try:
            await bot.ban_chat_member(message.chat.id, target_id, until_date=until_dt)
            dur_note = f" на <b>{_format_duration(duration)}</b>" if duration else ""
            reason_note = f"\n📝 Причина: <i>{escape_html(reason)}</i>" if reason else ""
            await message.answer(
                f"🚫 <b>{escape_html(sender.display_name)}</b> забанил <b>{escape_html(target_name)}</b>{dur_note}.{reason_note}",
                parse_mode="HTML",
            )
        except Exception:
            await message.answer("Не удалось забанить пользователя.")

    @router.message(Command("unban"))
    async def unban_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return

        target_id, target_name, _ = _resolve_target_and_tail(message, db, command.args)
        if not target_id:
            await message.answer("Используй /unban в реплае или укажи <id|@username>.")
            return

        try:
            await bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
            await message.answer(
                f"✅ <b>{escape_html(sender.display_name)}</b> разбанил <b>{escape_html(target_name)}</b>.",
                parse_mode="HTML",
            )
        except Exception:
            await message.answer("Не удалось разбанить пользователя.")

    @router.message(Command("kick"))
    async def kick_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return

        target_id, target_name, tail = _resolve_target_and_tail(message, db, command.args)
        if not target_id:
            await message.answer("Используй /kick в реплае или укажи @username / ID [причина].")
            return
        if await is_admin(bot, message.chat.id, target_id):
            await message.answer("Этого пользователя кикнуть нельзя.")
            return

        reason = " ".join(tail).strip()
        try:
            await bot.ban_chat_member(
                message.chat.id,
                target_id,
                until_date=datetime.now(timezone.utc) + timedelta(seconds=45),
            )
            await bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
            reason_note = f"\n📝 Причина: <i>{escape_html(reason)}</i>" if reason else ""
            await message.answer(
                f"👢 <b>{escape_html(sender.display_name)}</b> исключил <b>{escape_html(target_name)}</b> из чата.{reason_note}",
                parse_mode="HTML",
            )
        except Exception:
            await message.answer("Не удалось исключить пользователя.")

    @router.message(Command("mute"))
    async def mute_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return

        target_id, target_name, tail = _resolve_target_and_tail(message, db, command.args)
        if not target_id:
            await message.answer("Используй /mute в реплае или укажи @username / ID [10m|2h|1d] [причина].")
            return
        if await is_admin(bot, message.chat.id, target_id):
            await message.answer("Этого пользователя нельзя мутить.")
            return

        duration = parse_duration(tail[0]) if tail else None
        reason_tokens = tail[1:] if (duration and tail) else tail
        if duration is None:
            duration = timedelta(minutes=60)
        reason = " ".join(reason_tokens).strip()
        until_dt = datetime.now(timezone.utc) + duration

        try:
            await bot.restrict_chat_member(
                message.chat.id,
                target_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_dt,
            )
            reason_note = f"\n📝 Причина: <i>{escape_html(reason)}</i>" if reason else ""
            await message.answer(
                f"🤐 <b>{escape_html(sender.display_name)}</b> выдал мут <b>{escape_html(target_name)}</b> "
                f"на <b>{_format_duration(duration)}</b>.{reason_note}",
                parse_mode="HTML",
            )
        except Exception:
            await message.answer("Не удалось выдать мут.")

    @router.message(Command("unmute"))
    async def unmute_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return

        target_id, target_name, _ = _resolve_target_and_tail(message, db, command.args)
        if not target_id:
            await message.answer("Используй /unmute в реплае или укажи @username / ID.")
            return

        try:
            await bot.restrict_chat_member(
                message.chat.id,
                target_id,
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
                ),
            )
            await message.answer(
                f"🔊 <b>{escape_html(sender.display_name)}</b> снял мут с <b>{escape_html(target_name)}</b>.",
                parse_mode="HTML",
            )
        except Exception:
            await message.answer("Не удалось снять мут.")

    @router.message(Command("warn"))
    async def warn_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return

        target_id, target_name, tail = _resolve_target_and_tail(message, db, command.args)
        if not target_id:
            await message.answer("Используй /warn в реплае или укажи @username / ID [причина].")
            return
        if await is_admin(bot, message.chat.id, target_id):
            await message.answer("Администратору нельзя выдать предупреждение.")
            return

        reason = " ".join(tail).strip()
        target_user = db.get_user_by_platform_id(message.chat.id, target_id)
        if not target_user:
            target_user = db.get_or_create_user(
                message.chat.id,
                Sender(user_id=target_id, first_name=target_name.lstrip("@"), username=None),
            )

        updated = db.apply_warn(target_user)
        warns_now = updated.warns if updated else target_user.warns + 1
        warn_limit = db.settings.warn_limit
        reason_note = f"\n📝 Причина: <i>{escape_html(reason)}</i>" if reason else ""

        if warns_now >= warn_limit:
            db.clear_warns(updated or target_user)
            mute_delta = timedelta(hours=1)
            muted_ok = False
            try:
                await bot.restrict_chat_member(
                    message.chat.id,
                    target_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=datetime.now(timezone.utc) + mute_delta,
                )
                muted_ok = True
            except Exception:
                pass
            punish_text = "Выдан авто-мут на 1 ч., счётчик варнов сброшен." if muted_ok else "Счётчик варнов сброшен."
            await message.answer(
                f"⚠️ <b>{escape_html(target_name)}</b> получил предупреждение "
                f"<b>{warns_now}/{warn_limit}</b> от <b>{escape_html(sender.display_name)}</b>.{reason_note}\n"
                f"🔨 {punish_text}",
                parse_mode="HTML",
            )
            return

        await message.answer(
            f"⚠️ <b>{escape_html(target_name)}</b> получил предупреждение "
            f"<b>{warns_now}/{warn_limit}</b> от <b>{escape_html(sender.display_name)}</b>.{reason_note}",
            parse_mode="HTML",
        )

    @router.message(Command("unwarn", "resetwarns"))
    async def unwarn_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return

        target_id, target_name, tail = _resolve_target_and_tail(message, db, command.args)
        if not target_id:
            await message.answer("Используй /unwarn в реплае или укажи @username / ID.")
            return

        target_user = db.get_user_by_platform_id(message.chat.id, target_id)
        if not target_user or target_user.warns <= 0:
            await message.answer(f"У <b>{escape_html(target_name)}</b> нет активных предупреждений.", parse_mode="HTML")
            return

        clear_all = (command.command == "resetwarns") or (tail and tail[0].lower() in {"all", "все", "всё"})
        if clear_all or target_user.warns <= 1:
            db.clear_warns(target_user)
            new_warns = 0
        else:
            new_warns = target_user.warns - 1
            db.update_user(target_user.id, {"warns": new_warns})

        await message.answer(
            f"✅ <b>{escape_html(sender.display_name)}</b> снял предупреждение с <b>{escape_html(target_name)}</b> "
            f"(осталось: <b>{new_warns}/{db.settings.warn_limit}</b>).",
            parse_mode="HTML",
        )

    @router.message(Command("warns"))
    async def warns_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        target_id, target_name, _ = _resolve_target_and_tail(message, db, command.args)
        if not target_id:
            target_id = sender.user_id
            target_name = sender.display_name

        target_user = db.get_user_by_platform_id(message.chat.id, target_id)
        warns_count = target_user.warns if target_user else 0
        await message.answer(
            f"📋 Предупреждения <b>{escape_html(target_name)}</b>: <b>{warns_count}/{db.settings.warn_limit}</b>.",
            parse_mode="HTML",
        )

    @router.message(Command("rules"))
    async def rules_command(message: Message) -> None:
        rules = db.get_chat_rules(message.chat.id)
        if not rules:
            await message.answer(
                "📜 <b>Правила чата пока не заданы.</b>\n"
                "<i>Администратор может установить их командой</i> <code>/setrules текст правил</code>.",
                parse_mode="HTML",
            )
            return
        await message.answer(
            f"📜 <b>Правила чата:</b>\n\n{escape_html(rules)}",
            parse_mode="HTML",
        )

    @router.message(Command("setrules"))
    async def setrules_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return
        text = (command.args or "").strip()
        if not text and message.reply_to_message:
            text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
        if not text:
            await message.answer("Использование: /setrules <текст правил> (или ответом на сообщение).")
            return
        db.set_chat_rules(message.chat.id, text, updated_by=sender.user_id)
        await message.answer("✅ Правила чата успешно сохранены! Посмотреть: /rules")

    @router.message(Command("report"))
    async def report_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not message.reply_to_message:
            await message.answer("Ответьте командой /report на сообщение нарушителя.")
            return
        offender = get_sender_data(message.reply_to_message)
        if offender.user_id == sender.user_id:
            await message.answer("Нельзя отправить жалобу на самого себя.")
            return
        reason = (command.args or "").strip() or "Нарушение правил чата"
        db.create_feedback(
            message.chat.id,
            sender.user_id,
            sender.display_name,
            "complaint",
            f"Репорт на {offender.display_name} (msg_id={message.reply_to_message.message_id}): {reason}",
        )
        await message.answer(
            f"🚨 <b>Жалоба отправлена администраторам!</b>\n"
            f"👤 Отправитель: <b>{escape_html(sender.display_name)}</b>\n"
            f"🎯 Нарушитель: <b>{escape_html(offender.display_name)}</b>\n"
            f"📝 Причина: <i>{escape_html(reason)}</i>",
            parse_mode="HTML",
        )

    @router.message(Command("purge", "clear"))
    async def purge_command(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Нет прав.")
            return

        count = 10
        if command.args and command.args.strip().isdigit():
            count = max(1, min(50, int(command.args.strip())))

        ids_to_delete: list[int] = [message.message_id]
        if message.reply_to_message:
            start_id = message.reply_to_message.message_id
            end_id = message.message_id
            span = list(range(start_id, end_id))[-50:]
            ids_to_delete.extend(span)
        else:
            for offset in range(1, count + 1):
                if message.message_id - offset > 0:
                    ids_to_delete.append(message.message_id - offset)

        deleted = 0
        for mid in sorted(set(ids_to_delete), reverse=True):
            try:
                await bot.delete_message(message.chat.id, mid)
                deleted += 1
            except Exception:
                continue

        await message.answer(f"🧹 Очищено сообщений: <b>{deleted}</b>.", parse_mode="HTML")

    @router.message(Command("banword"))
    async def banword(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            return
        if not command.args:
            await message.answer("Использование: /banword <слово>")
            return
        db.add_bad_word(message.chat.id, command.args.strip().lower())
        await message.answer("Слово добавлено в фильтр.")

    @router.message(Command("unbanword"))
    async def unbanword(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            return
        if not command.args:
            await message.answer("Использование: /unbanword <слово>")
            return
        db.remove_bad_word(message.chat.id, command.args.strip().lower())
        await message.answer("Слово удалено из фильтра.")

    @router.message(Command("listwords"))
    async def listwords(message: Message) -> None:
        sender = get_sender_data(message)
        if not await is_admin(bot, message.chat.id, sender.user_id):
            return
        words = db.get_bad_words(message.chat.id)
        await message.answer(", ".join(words) if words else "Список пуст.")

    return router
