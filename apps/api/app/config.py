from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./ss_bot.db"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"
    task_deadline_reminder_hours: int = Field(default=24, ge=1, le=168)
    invite_reminder_minutes: int = Field(default=30, ge=5, le=1440)
    staging_task_cleanup_minutes: int | None = Field(default=None, ge=5, le=60)
    archive_retention_days: int = Field(default=365, ge=1, le=3650)
    archive_delete_warning_days: int = Field(default=30, ge=1, le=365)
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_webhook_url: str = ""
    telegram_webhook_path: str = "/webhook"
    telegram_webhook_secret: str = ""
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
    superadmin_telegram_ids: str = ""

    @field_validator("telegram_api_id", "staging_task_cleanup_minutes", mode="before")
    @classmethod
    def empty_optional_integer_is_unset(cls, value: object) -> object:
        """Allow documented optional integer settings to remain blank."""
        return None if value == "" else value

    @property
    def bootstrap_admin_ids(self) -> set[int]:
        return {
            int(value) for value in self.bootstrap_admin_telegram_ids.split(",") if value.strip()
        }

    @property
    def superadmin_ids(self) -> set[int]:
        return {int(value) for value in self.superadmin_telegram_ids.split(",") if value.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
