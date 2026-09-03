from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    owner_id: int
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "telegram_autofilter"
    force_sub_channel_id: int | None = None
    results_per_page: int = 8
    auto_delete_seconds: int = 0
    timezone: str = "Asia/Kolkata"
    storage_channel_id: int | None = None
    updates_channel_id: int | None = None
    deletion_log_channel_id: int | None = None
    # Public HTTPS address of the web service used for verification callbacks.
    # Example: https://your-service.koyeb.app (no trailing slash).
    verify_base_url: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
