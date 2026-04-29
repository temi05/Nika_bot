from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Sender:
    user_id: int
    first_name: str
    username: str | None = None
    is_bot: bool = False

    @property
    def display_name(self) -> str:
        return f"@{self.username}" if self.username else self.first_name


@dataclass(slots=True)
class ChatUser:
    id: int
    chat_id: int
    user_id: int
    first_name: str
    username: str | None
    xp: int
    level: int
    reputation: int
    warns: int
    last_message_time: int
    birthday: str | None = None
    bio: str | None = None
    ai_notes: str | None = None
    photo_url: str | None = None
    last_daily_claim: str | None = None
    last_warn_at: str | None = None
    flavor: str | None = None
    debt: int = 0
    last_loan_at: str | None = None
    jailed_until: str | None = None
    jail_reason: str | None = None
    steal_fail_streak: int = 0
    steal_success_streak: int = 0
    sign_price: int = 0

    @property
    def display_name(self) -> str:
        return f"@{self.username}" if self.username else self.first_name


@dataclass(slots=True)
class ChatSettings:
    chat_id: int
    link_filter_enabled: bool = True
    casino_jackpot: int = 0


@dataclass(slots=True)
class Reminder:
    id: int
    chat_id: int
    user_id: int
    text: str
    trigger_time: datetime
    user_name: str | None = None
    is_sent: bool = False


@dataclass(slots=True)
class MemoryRecord:
    fact: str
    source: str
    confidence: float = 0.7
    meta: dict[str, Any] | None = None


@dataclass(slots=True)
class VerificationChallenge:
    chat_id: int
    user_id: int
    code: str
    prompt_message_id: int
    created_at: datetime
    timeout_seconds: int = 120
    metadata: dict[str, Any] = field(default_factory=dict)
