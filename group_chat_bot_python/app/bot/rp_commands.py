from __future__ import annotations

import json
import random
from pathlib import Path

import httpx
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.admin import is_admin
from app.config import get_settings
from app.services.supabase_db import SupabaseDB
from app.utils import escape_html, get_sender_data


RP_GIFS_PATH = Path(__file__).with_name("rp_gifs.json")
RP_GIF_CACHE: dict[str, str] = {}


# Словарь RP-команд с их описанием и стоимостью
RP_ACTIONS = {
    # Базовые действия (бесплатно или дешево)
    "обнять": {
        "emoji": "🤗",
        "cost": 0,
        "self": "Обнял(а) себя сама 🤗",
        "template": "{sender} обнял(а) {target} 🤗",
    },
    "погладить": {
        "emoji": "👋",
        "cost": 0,
        "self": "Погладил(а) себя по головке 👋",
        "template": "{sender} погладил(а) {target} по головке 👋",
    },
    "поцеловать": {
        "emoji": "💋",
        "cost": 1,
        "self": "Поцеловал(а) себя в зеркало 💋",
        "template": "{sender} поцеловал(а) {target} 💋",
    },
    "пожать_руку": {
        "emoji": "🤝",
        "cost": 0,
        "self": "Пожал(а) свою руку... странно 🤔",
        "template": "{sender} пожал(а) руку {target} 🤝",
    },
    "взять_за_руку": {
        "emoji": "👫",
        "cost": 0,
        "self": "Взял(а) себя за руку... странно 🤔",
        "template": "{sender} взял(а) {target} за руку 👫",
    },
    "похлопать": {
        "emoji": "👏",
        "cost": 0,
        "self": "Похлопал(а) в ладоши себе 👏",
        "template": "{sender} похлопал(а) {target} по плечу 👏",
    },
    
    # Эмоциональные действия
    "поддержать": {
        "emoji": "💪",
        "cost": 0,
        "self": "Поддержал(а) себя морально 💪",
        "template": "{sender} поддержал(а) {target} 💪",
    },
    "утешить": {
        "emoji": "😢",
        "cost": 0,
        "self": "Утешил(а) себя... не помогло 😢",
        "template": "{sender} утешил(а) {target} 😢",
    },
    "порадоваться": {
        "emoji": "🎉",
        "cost": 0,
        "self": "Порадовался(ась) за себя 🎉",
        "template": "{sender} порадовался(ась) за {target} 🎉",
    },
    "поздравить": {
        "emoji": "🎊",
        "cost": 0,
        "self": "Поздравил(а) себя 🎊",
        "template": "{sender} поздравил(а) {target} 🎊",
    },
    
    # Игривые действия
    "пощекотать": {
        "emoji": "😆",
        "cost": 0,
        "self": "Пощекотал(а) себя... не смешно 😆",
        "template": "{sender} пощекотал(а) {target} 😆",
    },
    "подразнить": {
        "emoji": "😏",
        "cost": 0,
        "self": "Подразнил(а) своё отражение 😏",
        "template": "{sender} подразнил(а) {target} 😏",
    },
    "шлепнуть": {
        "emoji": "😵",
        "cost": 2,
        "self": "Шлёпнул(а) себя... больно 😵",
        "template": "{sender} шлёпнул(а) {target} 😵",
    },
    "укусить": {
        "emoji": "🦷",
        "cost": 1,
        "self": "Укусил(а) себя за палец 🦷",
        "template": "{sender} укусил(а) {target} 🦷",
    },
    "кусь": {
        "emoji": "🦷",
        "cost": 1,
        "self": "Сделал(а) себе кусь 🦷",
        "template": "{sender} сделал(а) кусь {target} 🦷",
    },
    "убить": {
        "emoji": "💀",
        "cost": 5,
        "self": "Самоликвидация... шучу 💀",
        "template": "{sender} убил(а) {target} 💀",
    },
    "ударить": {
        "emoji": "👊",
        "cost": 1,
        "self": "Ударил(а) себя... зачем? 👊",
        "template": "{sender} ударил(а) {target} 👊",
    },
    "покормить": {
        "emoji": "🍎",
        "cost": 0,
        "self": "Сама покушал(а) 🍎",
        "template": "{sender} покормил(а) {target} 🍎",
    },
    "прижать": {
        "emoji": "🤱",
        "cost": 1,
        "self": "Прижал(а) к себе подушку 🤱",
        "template": "{sender} нежно прижал(а) {target} к себе 🤱",
    },
    "облизать": {
        "emoji": "👅",
        "cost": 2,
        "self": "Облизал(а) губы 👅",
        "template": "{sender} облизал(а) {target} 👅",
    },
    "лизнуть": {
        "emoji": "👅",
        "cost": 1,
        "self": "Лизнул(а) себя за руку 👅",
        "template": "{sender} лизнул(а) {target} 👅",
    },
    "шепнуть": {
        "emoji": "💬",
        "cost": 0,
        "self": "Шепнул(а) себе под нос 💬",
        "template": "{sender} шепнул(а) на ушко {target}: <i>{phrase}</i> 💬",
    },
}


RP_GIF_QUERIES = {
    "обнять": ["anime hug", "cute hug anime"],
    "погладить": ["anime head pat", "cute headpat anime"],
    "поцеловать": ["anime kiss", "cute anime kiss"],
    "пожать_руку": ["anime handshake", "handshake"],
    "взять_за_руку": ["anime holding hands", "holding hands anime"],
    "похлопать": ["anime pat shoulder", "shoulder pat"],
    "поддержать": ["anime cheer up", "you can do it anime"],
    "утешить": ["anime comfort hug", "comforting anime"],
    "порадоваться": ["anime happy celebration", "anime yay"],
    "поздравить": ["anime congratulations", "anime celebration"],
    "пощекотать": ["anime tickle", "tickling anime"],
    "подразнить": ["anime teasing", "smug anime"],
    "шлепнуть": ["anime slap", "anime bonk"],
    "укусить": ["anime bite", "cute anime bite"],
    "кусь": ["anime bite", "cute anime bite"],
    "убить": ["anime dramatic death", "anime knockout"],
    "ударить": ["anime punch", "anime bonk"],
    "покормить": ["anime feeding", "anime food sharing"],
    "прижать": ["anime cuddle hug", "anime embrace"],
    "облизать": ["anime lick", "anime silly lick"],
    "лизнуть": ["anime lick", "anime silly lick"],
    "шепнуть": ["anime whisper", "whisper anime"],
}


def _load_rp_gifs() -> dict[str, str]:
    if not RP_GIFS_PATH.exists():
        return {}
    try:
        raw = json.loads(RP_GIFS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[RP:gifs_load_error] path={RP_GIFS_PATH} error={exc}")
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(action).strip().lower(): str(animation).strip()
        for action, animation in raw.items()
        if str(action).strip() and str(animation).strip()
    }


def _save_rp_gifs(gifs: dict[str, str]) -> bool:
    payload = {action: gifs.get(action, "") for action in RP_ACTIONS}
    try:
        RP_GIFS_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        print(f"[RP:gifs_save_error] path={RP_GIFS_PATH} error={exc}")
        return False


def _extract_animation_id(message: Message) -> str | None:
    if message.animation:
        return message.animation.file_id
    if message.document and (message.document.mime_type or "").lower() in {"image/gif", "video/mp4"}:
        return message.document.file_id
    if message.sticker and (message.sticker.is_animated or message.sticker.is_video):
        return message.sticker.file_id
    if message.video:
        return message.video.file_id
    return None


RP_ACTION_TO_NEKOS_BEST = {
    "обнять": "hug",
    "погладить": "pat",
    "поцеловать": "kiss",
    "пожать_руку": "handshake",
    "взять_за_руку": "handhold",
    "похлопать": "clap",
    "поддержать": "thumbsup",
    "утешить": "cuddle",
    "порадоваться": "happy",
    "поздравить": "wave",
    "пощекотать": "tickle",
    "подразнить": "smug",
    "шлепнуть": "slap",
    "укусить": "bite",
    "кусь": "bite",
    "убить": "shoot",
    "ударить": "punch",
    "покормить": "feed",
    "прижать": "cuddle",
    "облизать": "bleh",
    "лизнуть": "bleh",
    "шепнуть": "lurk",
}


async def _find_nekos_best_gif(action_name: str) -> str | None:
    if action_name in RP_GIF_CACHE:
        return RP_GIF_CACHE[action_name]

    category = RP_ACTION_TO_NEKOS_BEST.get(action_name)
    if not category:
        category = action_name.lower().strip()
        supported_categories = {
            "angry", "baka", "bite", "bleh", "blowkiss", "blush", "bonk", "bored", "carry", "clap",
            "confused", "cry", "cuddle", "dance", "facepalm", "feed", "handhold", "handshake",
            "happy", "highfive", "hug", "kabedon", "kick", "kiss", "lap", "laugh", "lurk", "nod",
            "nom", "nope", "nyan", "pat", "peck", "poke", "pout", "punch", "run", "salute", "shake",
            "shoot", "shrug", "sip", "slap", "sleep", "smile", "smug", "spin", "stare", "tableflip",
            "think", "thumbsup", "tickle", "wave", "wink", "yawn", "yeet"
        }
        if category not in supported_categories:
            clean_act = category.casefold()
            if any(w in clean_act for w in ["смех", "laugh", "lol", "haha", "хаха", "ору"]):
                category = "laugh"
            elif any(w in clean_act for w in ["груст", "плач", "cry", "sad"]):
                category = "cry"
            elif any(w in clean_act for w in ["зло", "angry", "rage", "бесит"]):
                category = "angry"
            elif any(w in clean_act for w in ["дума", "think", "хмм"]):
                category = "think"
            elif any(w in clean_act for w in ["привет", "wave", "hi", "hello"]):
                category = "wave"
            elif any(w in clean_act for w in ["сон", "sleep", "tired"]):
                category = "sleep"
            elif any(w in clean_act for w in ["подмиг", "wink"]):
                category = "wink"
            elif any(w in clean_act for w in ["смущ", "blush", "стыд"]):
                category = "blush"
            else:
                category = "happy"

    headers = {"User-Agent": "NikaBot/1.0 (https://t.me/NikaBot)"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(f"https://nekos.best/api/v2/{category}", headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        print(f"[RP:nekos_best_error] action={action_name} category={category} error={exc}")
        return None

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return None

    url = str(results[0].get("url") or "").strip()
    if url:
        RP_GIF_CACHE[action_name] = url
        return url
    return None


async def _answer_rp(message: Message, text: str, *, action_name: str, parse_mode: str | None = None) -> None:
    animation = _load_rp_gifs().get(action_name) or await _find_nekos_best_gif(action_name)
    if not animation:
        await message.answer(text, parse_mode=parse_mode)
        return
    try:
        await message.answer_animation(animation=animation, caption=text, parse_mode=parse_mode)
    except Exception as exc:
        print(f"[RP:gif_send_error] action={action_name} error={exc}")
        await message.answer(text, parse_mode=parse_mode)


def build_rp_router(db: SupabaseDB) -> Router:
    router = Router(name="rp")

    @router.message(Command("rp"))
    async def rp_handler(message: Message, command: CommandObject) -> None:
        """Обработчик команды /rp - показывает справку или выполняет действие"""
        # Если нет аргументов - показываем справку
        if not command.args or not command.args.strip():
            lines = [
                "<b>🎭 RP-команды</b>",
                "Использование: /rp &lt;действие&gt; в ответ на сообщение",
                "",
                "<b>Базовые:</b>",
                "• обнять, погладить, поцеловать",
                "• пожать_руку, взять_за_руку, похлопать",
                "",
                "<b>Эмоциональные:</b>",
                "• поддержать, утешить, порадоваться, поздравить",
                "",
                "<b>Игривые:</b>",
                "• пощекотать, подразнить, шлепнуть, укусить, лизнуть",
                "",
                "<b>Особые:</b>",
                "• прижать, облизать, шепнуть &lt;текст&gt;",
                "",
                "<b>GIF:</b>",
                "• /rpgifs — что уже настроено",
                "• /setrpgif действие — сохранить GIF из реплая",
                "",
                "<i>Некоторые действия требуют печенек.</i>",
            ]
            await message.answer("\n".join(lines), parse_mode="HTML")
            return

        # Проверяем, есть ли reply на сообщение
        if not message.reply_to_message:
            await message.answer("Использование: /rp &lt;действие&gt; в ответ на сообщение пользователя", parse_mode="HTML")
            return

        # Парсим команду и текст (для шепнуть)
        parts = command.args.strip().split(maxsplit=1)
        action_name = parts[0].lower()
        extra_phrase = parts[1] if len(parts) > 1 else None

        # Проверяем существование действия
        if action_name not in RP_ACTIONS:
            await message.answer(
                f"Неизвестное действие: <code>{action_name}</code>\n"
                f"Напиши /rp для списка команд.",
                parse_mode="HTML",
            )
            return

        action = RP_ACTIONS[action_name]
        sender = get_sender_data(message)
        target = get_sender_data(message.reply_to_message)

        # Проверка на самого себя
        if sender.user_id == target.user_id:
            await _answer_rp(message, action["self"], action_name=action_name)
            return

        # Проверка стоимости
        cost = action.get("cost", 0)
        if cost > 0:
            sender_user = db.get_or_create_user(message.chat.id, sender)
            if sender_user.reputation < cost:
                await message.answer(
                    f"😔 Нужно {cost} печенек для этого действия. "
                    f"У тебя: {sender_user.reputation} 🍪",
                    parse_mode="HTML",
                )
                return
            # Списываем печеньки
            db.add_reputation(sender_user, -cost)

        # Формируем результат
        if action_name == "шепнуть" and extra_phrase:
            result = action["template"].format(
                sender=escape_html(sender.display_name),
                target=escape_html(target.display_name),
                phrase=escape_html(extra_phrase),
            )
        else:
            result = action["template"].format(
                sender=escape_html(sender.display_name),
                target=escape_html(target.display_name),
            )

        await _answer_rp(message, result, action_name=action_name, parse_mode="HTML")

    @router.message(Command("setrpgif"))
    async def set_rp_gif_handler(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Настраивать RP-гивки могут только админы.")
            return
        if not command.args or not command.args.strip():
            await message.answer("Использование: ответь на GIF командой <code>/setrpgif действие</code>", parse_mode="HTML")
            return

        action_name = command.args.strip().split(maxsplit=1)[0].lower()
        if action_name not in RP_ACTIONS:
            await message.answer(
                f"Неизвестное действие: <code>{escape_html(action_name)}</code>\n"
                f"Напиши /rp для списка действий.",
                parse_mode="HTML",
            )
            return
        if not message.reply_to_message:
            await message.answer("Ответь этой командой на GIF/анимацию: <code>/setrpgif обнять</code>", parse_mode="HTML")
            return

        animation_id = _extract_animation_id(message.reply_to_message)
        if not animation_id:
            await message.answer("В реплае не вижу GIF, animation, video-sticker или короткое mp4-видео.")
            return

        gifs = _load_rp_gifs()
        gifs[action_name] = animation_id
        if not _save_rp_gifs(gifs):
            await message.answer("Не смог сохранить GIF в файл конфигурации.")
            return
        RP_GIF_CACHE.pop(action_name, None)
        await message.answer(
            f"✅ GIF для <code>{escape_html(action_name)}</code> сохранена.\n"
            f"Проверка: ответь кому-нибудь <code>/rp {escape_html(action_name)}</code>",
            parse_mode="HTML",
        )

    @router.message(Command("delrpgif"))
    async def del_rp_gif_handler(message: Message, command: CommandObject) -> None:
        sender = get_sender_data(message)
        if not await is_admin(message.bot, message.chat.id, sender.user_id):
            await message.answer("⛔ Настраивать RP-гивки могут только админы.")
            return
        if not command.args or not command.args.strip():
            await message.answer("Использование: <code>/delrpgif действие</code>", parse_mode="HTML")
            return

        action_name = command.args.strip().split(maxsplit=1)[0].lower()
        if action_name not in RP_ACTIONS:
            await message.answer(f"Неизвестное действие: <code>{escape_html(action_name)}</code>", parse_mode="HTML")
            return

        gifs = _load_rp_gifs()
        gifs[action_name] = ""
        if not _save_rp_gifs(gifs):
            await message.answer("Не смог сохранить файл конфигурации.")
            return
        RP_GIF_CACHE.pop(action_name, None)
        await message.answer(f"🧹 GIF для <code>{escape_html(action_name)}</code> очищена.", parse_mode="HTML")

    @router.message(Command("rpgifs"))
    async def rp_gifs_handler(message: Message) -> None:
        gifs = _load_rp_gifs()
        configured = [action for action in RP_ACTIONS if gifs.get(action)]
        missing = [action for action in RP_ACTIONS if not gifs.get(action)]
        lines = [
            "🎭 <b>RP-гивки</b>",
            f"Настроено: <b>{len(configured)}</b> / <b>{len(RP_ACTIONS)}</b>",
            "",
        ]
        if configured:
            lines.append("<b>Есть:</b> " + ", ".join(configured))
        if missing:
            lines.append("<b>Нет:</b> " + ", ".join(missing))
        lines.extend([
            "",
            "Как добавить: отправь GIF, ответь на неё <code>/setrpgif обнять</code>",
            "Как удалить: <code>/delrpgif обнять</code>",
        ])
        await message.answer("\n".join(lines), parse_mode="HTML")

    return router
