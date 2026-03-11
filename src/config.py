"""Centralised configuration.

Loads all environment variables from a .env file (or the real environment).
Every other module imports from here — never from os.environ directly.

Usage:
    from src.config import settings

    print(settings.jira_base_url)
"""

from functools import lru_cache
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Google ────────────────────────────────────────
    # Primary key for Gemini / google-genai SDK
    gemini_api_key: SecretStr

    # Optional: separate key for other Google services
    google_api_key: SecretStr | None = None

    # ── Jira ─────────────────────────────────────────
    jira_base_url: str
    jira_email: str
    jira_api_token: SecretStr

    # ── App ──────────────────────────────────────────
    app_env: str = "development"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Use this as a FastAPI dependency or call it directly in scripts.

        from src.config import get_settings
        settings = get_settings()
    """
    return Settings()


# Module-level singleton for convenience in non-FastAPI code
settings = get_settings()
