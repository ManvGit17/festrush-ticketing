"""Settings, read from the environment (prefix ``FESTRUSH_``) or a local ``.env`` file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

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

    # Threads each web process uses to run the (synchronous) route handlers. The default is
    # anyio's 40. See docs/architecture.md, experiment E0, for what changing it does.
    threadpool_size: int = Field(default=40, ge=1)

    pool_min_size: int = Field(default=2, ge=1)
    pool_max_size: int = Field(default=20, ge=1)

    # Password guessing: this many failures per (username, client) inside the window blocks
    # further attempts until the oldest one ages out.
    login_max_failures: int = Field(default=5, ge=1)
    login_window_minutes: int = Field(default=10, ge=1)

    # How a seat is claimed. See docs/concurrency.md for what each one does and costs.
    #   naive          read the count, then insert: the baseline that can oversell
    #   row_lock       lock the event row for the whole claim
    #   atomic_update  one conditional UPDATE of a counter on the event row
    #   skip_locked    take a free seat row nobody else has locked
    inventory_strategy: Literal["naive", "row_lock", "atomic_update", "skip_locked"] = "atomic_update"
    # Widens the gap between the naive strategy's check and its insert, as a slow network hop
    # or extra business logic would. Only the naive strategy reads it.
    naive_delay_ms: int = Field(default=0, ge=0)

    # The waiting room: off by default. When on, students must be admitted before they can reserve.
    waiting_room_enabled: bool = False
    waiting_room_mode: Literal["fifo", "lottery"] = "fifo"
    waiting_room_admit_per_second: int = Field(default=100, ge=1)
    waiting_room_admit_minutes: int = Field(default=5, ge=1)

    hold_minutes: int = Field(default=10, ge=1)
    # A hold with a payment in flight is kept this long past its expiry before it is released.
    payment_grace_seconds: int = Field(default=120, ge=0)
    # How long a payment may sit unsettled before the worker asks the provider what happened.
    payment_reconcile_seconds: int = Field(default=30, ge=0)

    # Send traces here (an OTLP/HTTP endpoint such as http://jaeger:4318). Unset means no tracing.
    otlp_endpoint: str | None = None
    worker_metrics_port: int = Field(default=9102, ge=1)

    # Set to use the payments service instead of the in-process mock provider.
    payments_url: str | None = None
    payments_timeout_seconds: float = Field(default=3.0, gt=0)
    payments_port: int = Field(default=8070, ge=1)

    # The mock payment provider. Rates are fractions between 0 and 1; outcomes are decided by
    # the idempotency key, so a retry of the same payment always gets the same answer.
    mockpay_latency_ms: int = Field(default=0, ge=0)
    mockpay_decline_rate: float = Field(default=0.0, ge=0, le=1)
    mockpay_timeout_rate: float = Field(default=0.0, ge=0, le=1)

    # Experiment E3 (docs/architecture.md): send the confirmation inside the payment request
    # instead of through the outbox. Slower and less safe; kept so the cost can be measured.
    notify_inline: bool = False

    worker_poll_ms: int = Field(default=200, ge=10)
    worker_handler_delay_ms: int = Field(default=0, ge=0)  # test hook: widen the crash window
    outbox_max_attempts: int = Field(default=8, ge=1)

    # Cheap Argon2 parameters so the test suite and CI do not spend minutes hashing.
    # Never enable this outside tests.
    insecure_fast_hashing: bool = False

    migrations_dir: Path = REPO_ROOT / "db" / "migrations"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # secret_key comes from the environment
