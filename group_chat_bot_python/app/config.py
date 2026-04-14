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
    ai_max_tokens: int = Field(default=700, alias="AI_MAX_TOKENS")
    ai_timeout_seconds: int = Field(default=45, alias="AI_TIMEOUT_SECONDS")

    memory_provider: str = Field(default="database", alias="MEMORY_PROVIDER")
    lightrag_base_url: str = Field(default="http://127.0.0.1:9621", alias="LIGHTRAG_BASE_URL")
    lightrag_api_key: str | None = Field(default=None, alias="LIGHTRAG_API_KEY")
    lightrag_query_mode: str = Field(default="hybrid", alias="LIGHTRAG_QUERY_MODE")
    lightrag_workspace: str | None = Field(default=None, alias="LIGHTRAG_WORKSPACE")
    lightrag_timeout_seconds: int = Field(default=30, alias="LIGHTRAG_TIMEOUT_SECONDS")

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
