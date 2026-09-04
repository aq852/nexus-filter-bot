from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    owner_id: int
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "telegram_autofilter"
    # Optional separate MongoDB database used only for indexed files and search.
    secondary_mongodb_uri: str | None = None
    secondary_mongodb_database: str | None = None
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
    tmdb_api_key: str | None = None
    tmdb_read_access_token: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator(
        "force_sub_channel_id",
        "storage_channel_id",
        "updates_channel_id",
        "deletion_log_channel_id",
        mode="before",
    )
    @classmethod
    def blank_optional_ids_are_disabled(cls, value: object) -> object:
        """Treat blank values in a copied .env file as unset optional IDs."""
        return None if isinstance(value, str) and not value.strip() else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
