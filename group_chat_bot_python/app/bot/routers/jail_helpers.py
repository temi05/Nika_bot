"""
Общие хелперы для jail/тюрьмы.
Используются в economy.py, games.py, profile_ai.py и general_admin.py.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from aiogram.types import Message

from app.utils import escape_html


def parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def jail_remaining(db, user) -> timedelta | None:
    """Возвращает оставшееся время тюрьмы или None если свободен."""
    jailed_until = parse_iso_dt(user.jailed_until)
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


def format_jail_remaining(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    minutes = seconds // 60
    if minutes < 60:
        return f"{max(1, minutes)} мин."
    return f"{minutes // 60} ч. {minutes % 60} мин."


def jail_user(db, user, minutes: int, reason: str) -> None:
    db.update_user(
        user.id,
        {
            "jailed_until": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
            "jail_reason": reason,
        },
    )


def maybe_jail_for_overdue_debt(db, user) -> None:
    if user.debt <= 0 or jail_remaining(db, user):
        return
    active_debts = db.get_active_debts_for_borrower(user.chat_id, user.user_id)
    now = datetime.now(timezone.utc)
    has_overdue = any((parse_iso_dt(debt.get("due_at")) or now) < now for debt in active_debts)
    if has_overdue:
        jail_user(db, user, 180, "просроченный долг")


async def deny_if_jailed(db, message: Message, user, action_name: str) -> bool:
    """Проверяет, в тюрьме ли пользователь. Если да — отвечает сообщением и возвращает True."""
    maybe_jail_for_overdue_debt(db, user)
    user = db.get_user_by_platform_id(user.chat_id, user.user_id) or user
    remaining = jail_remaining(db, user)
    if not remaining:
        return False
    await message.answer(
        f"🚔 <b>Ты сейчас в тюрьме.</b>\n"
        f"Действие <code>{escape_html(action_name)}</code> недоступно ещё <b>{format_jail_remaining(remaining)}</b>.\n"
        f"Причина: <i>{escape_html(user.jail_reason or 'нарушение')}</i>\n\n"
        f"Можно выйти через <code>/bail</code>.",
        parse_mode="HTML",
    )
    return True


def loan_limit(user) -> int:
    """Максимальная сумма долга для пользователя."""
    return max(50, user.level * 25 + user.reputation)


def bail_cost(db, user) -> int:
    """Стоимость залога для выхода из тюрьмы."""
    base_cost = max(50, user.level * 20, int(user.debt * 0.35))
    wealth_part = int(user.reputation * 0.20)
    return base_cost + wealth_part


def pay_bail(db, user, cost: int) -> dict[str, int]:
    """Списывает залог и освобождает пользователя из тюрьмы."""
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


# Глобальные словари сессий (shared между роутерами)
LOAN_SESSIONS: dict[str, dict] = {}
BAIL_SESSIONS: dict[str, dict] = {}
