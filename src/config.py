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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
