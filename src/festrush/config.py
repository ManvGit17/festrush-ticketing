"""Settings, read from the environment (prefix ``FESTRUSH_``) or a local ``.env`` file."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FESTRUSH_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://festrush:festrush-dev@localhost:5440/festrush"
    # Signs CSRF tokens. Required: there is deliberately no default, so a forgotten value is a
    # startup error and not a shared secret.
    secret_key: SecretStr = Field(min_length=16)

    debug: bool = False
    cookie_secure: bool = False  # set to true behind HTTPS
    session_hours: int = Field(default=12, ge=1)

    pool_min_size: int = Field(default=2, ge=1)
    pool_max_size: int = Field(default=20, ge=1)

    # Password guessing: this many failures per (username, client) inside the window blocks
    # further attempts until the oldest one ages out.
    login_max_failures: int = Field(default=5, ge=1)
    login_window_minutes: int = Field(default=10, ge=1)

    # Cheap Argon2 parameters so the test suite and CI do not spend minutes hashing.
    # Never enable this outside tests.
    insecure_fast_hashing: bool = False

    migrations_dir: Path = REPO_ROOT / "db" / "migrations"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # secret_key comes from the environment
