from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import FSInputFile


class TelegramBackupService:
    def __init__(self, bot: Bot, backup_chat_id: int | str | None, data_dir: Path) -> None:
        self.bot = bot
        self.backup_chat_id = backup_chat_id
        self.data_dir = Path(data_dir)
        self.zip_path = self.data_dir.parent / "chroma_memory_backup.zip"
        self._lock = asyncio.Lock()

    def _log(self, event: str, **kwargs: Any) -> None:
        details = " ".join(f"{key}={value!r}" for key, value in kwargs.items())
        print(f"[TELEGRAM_BACKUP:{event}] {details}".strip())

    async def upload_backup(self, caption: str = "💾 Автоматический бэкап ChromaDB") -> bool:
        """Архивирует папку ChromaDB и выгружает zip в Telegram"""
        if not self.backup_chat_id:
            self._log("skip_upload", reason="MEMORY_BACKUP_CHAT_ID_missing")
            return False

        async with self._lock:
            try:
                if not self.data_dir.exists():
                    self._log("skip_upload", reason="data_dir_not_found")
                    return False

                # Архивируем директорию
                if self.zip_path.exists():
                    os.remove(self.zip_path)

                shutil.make_archive(
                    base_name=str(self.zip_path.with_suffix("")),
                    format="zip",
                    root_dir=str(self.data_dir),
                )

                input_file = FSInputFile(str(self.zip_path), filename="chroma_memory_backup.zip")
                await self.bot.send_document(
                    chat_id=self.backup_chat_id,
                    document=input_file,
                    caption=caption,
                )
                self._log("upload_success", chat_id=self.backup_chat_id, zip_size=self.zip_path.stat().st_size)
                return True
            except Exception as e:
                self._log("upload_error", error=str(e))
                return False

    async def send_fact_card(self, fact_text: str, entity_name: str = "", total_facts: int = 0) -> None:
        """Отправляет наглядное уведомление о новом воспоминании в бэкап-чат"""
        if not self.backup_chat_id:
            return
        try:
            from app.utils import escape_html
            text = (
                f"🧠 <b>Новое воспоминание Ники!</b>\n\n"
                f"👤 <b>Субъект:</b> {escape_html(entity_name or 'Участник')}\n"
                f"📝 <b>Факт:</b> <i>{escape_html(fact_text)}</i>\n"
                f"📊 <b>Всего фактов в базе:</b> {total_facts}"
            )
            await self.bot.send_message(
                chat_id=self.backup_chat_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            self._log("send_fact_card_error", error=str(e))

    async def restore_from_zip_bytes(self, zip_bytes: bytes) -> bool:
        """Восстанавливает базу из полученных байтов ZIP (например по команде /restore_memory)"""
        async with self._lock:
            try:
                if self.zip_path.exists():
                    os.remove(self.zip_path)

                with open(self.zip_path, "wb") as f:
                    f.write(zip_bytes)

                if self.data_dir.exists():
                    shutil.rmtree(self.data_dir)

                shutil.unpack_archive(str(self.zip_path), str(self.data_dir), format="zip")
                self._log("restore_from_bytes_success", target=str(self.data_dir))
                return True
            except Exception as e:
                self._log("restore_from_bytes_error", error=str(e))
                return False
