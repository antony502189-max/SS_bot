from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./ss_bot.db"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_webhook_secret: str = ""
    mini_app_url: str = "http://localhost:5173"
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_service_phone: str = ""
    telegram_service_session_path: Path = Path("telegram-service.session")
    s3_endpoint_url: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "task-photos"
    s3_region: str = "us-east-1"
    s3_presign_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    bootstrap_admin_telegram_ids: str = ""

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def bootstrap_admin_ids(self) -> set[int]:
        return {
            int(value) for value in self.bootstrap_admin_telegram_ids.split(",") if value.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
