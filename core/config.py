from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Hospital Bulk Processing API"
    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = "sqlite+aiosqlite:///./paribus.db"

    hospital_api_base_url: str = "https://hospital-directory.onrender.com"
    upstream_concurrency: int = 10
    upstream_timeout_seconds: float = 60.0
    upstream_max_retries: int = 3
    upstream_backoff_seconds: float = 0.5

    max_csv_rows: int = 20
    max_csv_bytes: int = 1_000_000
    progress_poll_interval_seconds: float = 0.5

    @field_validator("database_url")
    @classmethod
    def use_async_driver(cls, url: str) -> str:
        # Render and most hosts hand out sync-style Postgres URLs.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+asyncpg://" + url.removeprefix(prefix)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()