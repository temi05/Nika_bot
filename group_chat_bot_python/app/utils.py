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


# Таблица транслитерации для нечёткого поиска (lat → cyr и cyr → lat)
_LAT_TO_CYR: dict[str, str] = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е",
    "yo": "ё", "zh": "ж", "z": "з", "i": "и", "j": "й", "k": "к",
    "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "r": "р",
    "s": "с", "t": "т", "u": "у", "f": "ф", "h": "х", "ts": "ц",
    "ch": "ч", "sh": "ш", "sch": "щ", "y": "ы", "yu": "ю", "ya": "я",
    "x": "кс", "q": "к", "w": "в",
}

_CYR_TO_LAT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "j", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


def transliterate_for_search(value: str | None) -> str:
    """Транслитерирует строку lat→cyr или оставляет как есть. Возвращает нижний регистр."""
    if not value:
        return ""
    text = value.strip().lower()

    # Если уже кириллица — пробуем cyr→lat транслит для хранения обоих вариантов
    has_cyr = bool(re.search(r"[а-яёА-ЯЁ]", text))
    has_lat = bool(re.search(r"[a-z]", text))

    if has_cyr and not has_lat:
        # Кириллический текст → латинский вариант
        result = ""
        i = 0
        while i < len(text):
            ch = text[i]
            result += _CYR_TO_LAT.get(ch, ch)
            i += 1
        return result.strip()

    if has_lat and not has_cyr:
        # Латинский текст → кириллический вариант
        result = ""
        i = 0
        while i < len(text):
            # Пробуем двухсимвольные комбинации сначала
            two = text[i:i+2]
            three = text[i:i+3]
            if three in _LAT_TO_CYR:
                result += _LAT_TO_CYR[three]
                i += 3
            elif two in _LAT_TO_CYR:
                result += _LAT_TO_CYR[two]
                i += 2
            else:
                ch = text[i]
                result += _LAT_TO_CYR.get(ch, ch)
                i += 1
        return result.strip()

    return text


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


def get_zodiac_sign(day: int, month: int) -> str:
    if (month == 3 and day >= 21) or (month == 4 and day <= 19):
        return "Овен ♈"
    elif (month == 4 and day >= 20) or (month == 5 and day <= 20):
        return "Телец ♉"
    elif (month == 5 and day >= 21) or (month == 6 and day <= 20):
        return "Близнецы ♊"
    elif (month == 6 and day >= 21) or (month == 7 and day <= 22):
        return "Рак ♋"
    elif (month == 7 and day >= 23) or (month == 8 and day <= 22):
        return "Лев ♌"
    elif (month == 8 and day >= 23) or (month == 9 and day <= 22):
        return "Дева ♍"
    elif (month == 9 and day >= 23) or (month == 10 and day <= 22):
        return "Весы ♎"
    elif (month == 10 and day >= 23) or (month == 11 and day <= 21):
        return "Скорпион ♏"
    elif (month == 11 and day >= 22) or (month == 12 and day <= 21):
        return "Стрелец ♐"
    elif (month == 12 and day >= 22) or (month == 1 and day <= 19):
        return "Козерог ♑"
    elif (month == 1 and day >= 20) or (month == 2 and day <= 18):
        return "Водолей ♒"
    else:
        return "Рыбы ♓"


def get_nika_compatibility(day: int, month: int) -> str:
    score = (day * 7 + month * 13) % 36 + 65
    return f"{score}%"


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
