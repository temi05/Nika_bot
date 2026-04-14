from __future__ import annotations

import html
import io
import random
import re
from datetime import datetime, timedelta, timezone

from aiogram.types import Message
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.models import Sender


def escape_html(value: str | None) -> str:
    return html.escape(value or "", quote=True)


def get_sender_data(message: Message) -> Sender:
    if message.sender_chat:
        username = getattr(message.sender_chat, "username", None)
        title = getattr(message.sender_chat, "title", None) or "Канал"
        return Sender(
            user_id=message.sender_chat.id,
            first_name=title,
            username=username,
            is_bot=False,
        )

    if not message.from_user:
        return Sender(user_id=0, first_name="Инкогнито")

    return Sender(
        user_id=message.from_user.id,
        first_name=message.from_user.first_name or "Инкогнито",
        username=message.from_user.username,
        is_bot=message.from_user.is_bot,
    )


def normalize_search_text(value: str | None) -> str:
    cleaned = re.sub(r"[^\w\s@-]+", " ", (value or "").lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_progress_bar(percent: float, size: int = 10) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(percent // (100 / size))
    return f"[{'█' * filled}{'░' * (size - filled)}]"


def parse_birthday_parts(value: str | None) -> tuple[int, int, int | None] | None:
    if not value:
        return None
    match = re.fullmatch(r"(\d{2})\.(\d{2})(?:\.(\d{4}))?", value.strip())
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else None
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    return day, month, year


def birthday_is_today(value: str | None, today: datetime | None = None) -> bool:
    parts = parse_birthday_parts(value)
    if not parts:
        return False
    day, month, _ = parts
    today = today or datetime.now()
    return today.day == day and today.month == month


def birthday_age_text(value: str | None, today: datetime | None = None) -> str:
    parts = parse_birthday_parts(value)
    if not parts:
        return ""
    _, _, year = parts
    if not year:
        return ""
    today = today or datetime.now()
    return f" ({today.year - year} лет)"


def human_timedelta(hours: int, minutes: int) -> str:
    return f"{hours} ч. {minutes} мин."


def generate_captcha_code(length: int = 4) -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(length))


def build_captcha_image(code: str) -> io.BytesIO:
    width, height = 220, 100
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)

    for _ in range(18):
        color = tuple(random.randint(120, 210) for _ in range(3))
        draw.line(
            (
                random.randint(0, width),
                random.randint(0, height),
                random.randint(0, width),
                random.randint(0, height),
            ),
            fill=color,
            width=random.randint(1, 3),
        )

    try:
        font = ImageFont.truetype("arial.ttf", 48)
    except Exception:
        font = ImageFont.load_default()

    x = 22
    for char in code:
        y = random.randint(16, 32)
        draw.text((x, y), char, font=font, fill=(20, 20, 20))
        x += 42

    image = image.filter(ImageFilter.SMOOTH)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def parse_simple_reminder_time(value: str, now: datetime | None = None) -> datetime | None:
    value = value.strip().lower()
    now = now or datetime.now(timezone.utc)

    in_match = re.fullmatch(r"(\d+)\s*(m|min|мин|h|hr|час|ч)", value)
    if in_match:
        amount = int(in_match.group(1))
        unit = in_match.group(2)
        if unit in {"m", "min", "мин"}:
            return now + timedelta(minutes=amount)
        return now + timedelta(hours=amount)

    at_match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if at_match:
        hour = int(at_match.group(1))
        minute = int(at_match.group(2))
        if hour > 23 or minute > 59:
            return None
        target = now.astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now.astimezone():
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)

    return None
