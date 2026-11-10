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

    async def send_fact_card(
        self,
        fact_text: str | int,
        entity_name: str = "",
        total_facts: int | list[str] = 0,
    ) -> None:
        """Отправляет наглядное уведомление о новом воспоминании в бэкап-чат"""
        if not self.backup_chat_id:
            return
        try:
            from app.utils import escape_html

            if isinstance(total_facts, (list, tuple)):
                facts_list = [str(item).strip() for item in total_facts if str(item).strip()]
                resolved_fact = "; ".join(facts_list) if facts_list else str(fact_text)
                resolved_total = len(facts_list)
            else:
                resolved_fact = str(fact_text).strip()
                try:
                    resolved_total = int(total_facts)
                except (TypeError, ValueError):
                    resolved_total = 0

            if not resolved_fact:
                return

            text = (
                f"🧠 <b>Новое воспоминание Ники!</b>\n\n"
                f"👤 <b>Субъект:</b> {escape_html(str(entity_name or 'Участник'))}\n"
                f"📝 <b>Факт:</b> <i>{escape_html(resolved_fact)}</i>\n"
                f"📊 <b>Всего фактов в базе:</b> {resolved_total}"
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
        import io
        import zipfile

        async with self._lock:
            try:
                target_root = self.data_dir.resolve()
                total_uncompressed = 0
                with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                    for member in zf.infolist():
                        member_path = Path(member.filename)
                        if member_path.is_absolute() or ".." in member_path.parts:
                            self._log("restore_rejected_unsafe_path", filename=member.filename)
                            return False
                        resolved_dest = (target_root / member_path).resolve()
                        if resolved_dest != target_root and target_root not in resolved_dest.parents:
                            self._log("restore_rejected_path_traversal", filename=member.filename)
                            return False
                        total_uncompressed += member.file_size
                        if total_uncompressed > 100 * 1024 * 1024:
                            self._log("restore_rejected_size_limit", size=total_uncompressed)
                            return False

                    if self.zip_path.exists():
                        os.remove(self.zip_path)
                    with open(self.zip_path, "wb") as f:
                        f.write(zip_bytes)

                    if self.data_dir.exists():
                        shutil.rmtree(self.data_dir)
                    self.data_dir.mkdir(parents=True, exist_ok=True)
                    zf.extractall(self.data_dir)

                self._log("restore_from_bytes_success", target=str(self.data_dir))
                return True
            except Exception as e:
                self._log("restore_from_bytes_error", error=str(e))
                return False
