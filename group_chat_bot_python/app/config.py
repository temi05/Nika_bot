from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    supabase_url: str = Field(alias="SUPABASE_URL")
    supabase_key: str = Field(alias="SUPABASE_KEY")

    bot_name: str = Field(default="НейроНика", alias="BOT_NAME")
    bot_username: str | None = Field(default=None, alias="BOT_USERNAME")
    webhook_secret_token: str = Field(default="change-me", alias="WEBHOOK_SECRET_TOKEN")
    render_external_url: str = Field(default="http://127.0.0.1:8080", alias="RENDER_EXTERNAL_URL")
    port: int = Field(default=8080, alias="PORT")

    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    polza_api_key: str | None = Field(default=None, validation_alias="POLZA_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")

    ai_model: str = Field(default="deepseek/deepseek-v4.1-flash", alias="AI_MODEL")
    ai_fallback_model: str = Field(default="google/gemini-2.5-flash", alias="AI_FALLBACK_MODEL")

    ai_image_model: str = Field(default="openai/gpt-image-1.5", alias="AI_IMAGE_MODEL")
    ai_temperature: float = Field(default=0.7, alias="AI_TEMPERATURE")
    ai_max_tokens: int = Field(default=500, alias="AI_MAX_TOKENS")

    ai_timeout_seconds: int = Field(default=45, alias="AI_TIMEOUT_SECONDS")
    ai_history_lines: int = Field(default=8, alias="AI_HISTORY_LINES")
    ai_persistent_history_enabled: bool = Field(default=True, alias="AI_PERSISTENT_HISTORY_ENABLED")
    ai_persistent_history_lines: int = Field(default=12, alias="AI_PERSISTENT_HISTORY_LINES")
    ai_compact_prompt: bool = Field(default=True, alias="AI_COMPACT_PROMPT")
    ai_group_cooldown_seconds: int = Field(default=12, alias="AI_GROUP_COOLDOWN_SECONDS")
    ai_min_message_len: int = Field(default=4, alias="AI_MIN_MESSAGE_LEN")
    ai_vision_enabled: bool = Field(default=True, alias="AI_VISION_ENABLED")
    ai_vision_max_images: int = Field(default=2, alias="AI_VISION_MAX_IMAGES")
    ai_vision_max_bytes: int = Field(default=4_000_000, alias="AI_VISION_MAX_BYTES")
    bot_personality_mode: str = Field(default="hard", alias="BOT_PERSONALITY_MODE")

    memory_model: str | None = Field(default=None, alias="MEMORY_MODEL")
    memory_extraction_enabled: bool = Field(default=True, alias="MEMORY_EXTRACTION_ENABLED")
    memory_extraction_max_facts: int = Field(default=6, alias="MEMORY_EXTRACTION_MAX_FACTS")
    memory_fact_min_confidence: float = Field(default=0.72, alias="MEMORY_FACT_MIN_CONFIDENCE")
    memory_retrieval_limit: int = Field(default=6, alias="MEMORY_RETRIEVAL_LIMIT")
    memory_capture_all_messages: bool = Field(default=False, alias="MEMORY_CAPTURE_ALL_MESSAGES")

    ai_image_price: int = Field(default=650, alias="AI_IMAGE_PRICE")
    ai_sign_price: int = Field(default=450, alias="AI_SIGN_PRICE")
    human_sign_min_price: int = Field(default=3000, alias="HUMAN_SIGN_MIN_PRICE")
    tenor_api_key: str | None = Field(default=None, alias="TENOR_API_KEY")
    tenor_client_key: str = Field(default="neuronika_rp", alias="TENOR_CLIENT_KEY")
    rp_gif_autosearch: bool = Field(default=False, alias="RP_GIF_AUTOSEARCH")

    # Провайдер памяти: "database" (Supabase) или "chroma" (ChromaDB + Telegram)
    memory_provider: str = Field(default="chroma", alias="MEMORY_PROVIDER")
    memory_backup_chat_id: int | str | None = Field(default=None, alias="MEMORY_BACKUP_CHAT_ID")

    link_filter_default: bool = Field(default=True, alias="LINK_FILTER_DEFAULT")
    warn_limit: int = Field(default=3, alias="WARN_LIMIT")
    warn_decay_days: int = Field(default=7, alias="WARN_DECAY_DAYS")
    daily_xp_min: int = Field(default=50, alias="DAILY_XP_MIN")
    daily_xp_max: int = Field(default=150, alias="DAILY_XP_MAX")
    superadmin_ids: list[int] | set[int] | int | str = Field(
        default_factory=lambda: [861713427],
        alias="SUPERADMIN_IDS",
    )

    @field_validator("superadmin_ids", mode="before")
    @classmethod
    def _parse_superadmin_ids(cls, value: Any) -> list[int]:
        if value is None or value == "":
            return [861713427]
        if isinstance(value, int):
            return [value]
        if isinstance(value, (list, tuple, set)):
            parsed: list[int] = []
            for item in value:
                try:
                    parsed.append(int(str(item).strip()))
                except (TypeError, ValueError):
                    continue
            return parsed or [861713427]
        if isinstance(value, str):
            cleaned = value.strip().strip("[]()")
            if not cleaned:
                return [861713427]
            parsed = []
            for part in cleaned.split(","):
                part = part.strip()
                if part:
                    try:
                        parsed.append(int(part))
                    except ValueError:
                        continue
            return parsed or [861713427]
        return [861713427]

    @property
    def effective_ai_api_key(self) -> str | None:
        return self.openai_api_key or self.polza_api_key

    @property
    def effective_ai_base_url(self) -> str:
        if self.polza_api_key and self.openai_base_url == "https://api.openai.com/v1":
            return "https://polza.ai/api/v1"
        return self.openai_base_url

    @property
    def effective_memory_model(self) -> str:
        return self.memory_model or self.ai_model


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_superadmin_ids() -> set[int]:
    try:
        return set(get_settings().superadmin_ids) | {861713427}
    except Exception:
        raw = os.getenv("SUPERADMIN_IDS", "")
        ids: set[int] = {861713427}
        for part in raw.strip().strip("[]()").split(","):
            part = part.strip()
            if part and part.lstrip("-").isdigit():
                ids.add(int(part))
        return ids
