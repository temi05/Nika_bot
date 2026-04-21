from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
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

    ai_model: str = Field(default="gpt-4o-mini", alias="AI_MODEL")
    ai_temperature: float = Field(default=0.7, alias="AI_TEMPERATURE")
    ai_max_tokens: int = Field(default=220, alias="AI_MAX_TOKENS")
    ai_timeout_seconds: int = Field(default=45, alias="AI_TIMEOUT_SECONDS")
    ai_history_lines: int = Field(default=4, alias="AI_HISTORY_LINES")
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

    # Провайдер памяти: только "database" (Supabase)
    memory_provider: str = Field(default="database", alias="MEMORY_PROVIDER")

    link_filter_default: bool = Field(default=True, alias="LINK_FILTER_DEFAULT")
    warn_limit: int = Field(default=3, alias="WARN_LIMIT")
    warn_decay_days: int = Field(default=7, alias="WARN_DECAY_DAYS")
    daily_xp_min: int = Field(default=50, alias="DAILY_XP_MIN")
    daily_xp_max: int = Field(default=150, alias="DAILY_XP_MAX")

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
