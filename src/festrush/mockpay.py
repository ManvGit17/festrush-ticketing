"""A mock payment provider. No real payments, no API keys, nothing leaves the machine.

It behaves like a real provider in the ways that matter for correctness:

* It is idempotent by key: charging the same key twice captures the money once.
* It can be slow, can decline, and can "time out": the charge is captured but the caller never
  hears back, so the caller has to ask later (``lookup``).
* Outcomes are decided from the key, so a retry gets the same answer a real provider would give.

Its ledger lives in the ``mock_charges`` table, written only here and through connections of its
own, as an external system's state would be.
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Literal

from festrush.config import Settings
from festrush.db import Pool, one

Outcome = Literal["succeeded", "declined"]


class ProviderTimeout(Exception):
    """The provider captured (or declined) the charge but the answer never arrived."""


@dataclass(frozen=True)
class MockProvider:
    pool: Pool
    latency_ms: int = 0
    decline_rate: float = 0.0
    timeout_rate: float = 0.0

    @classmethod
    def from_settings(cls, pool: Pool, settings: Settings) -> "MockProvider":
        return cls(
            pool,
            settings.mockpay_latency_ms,
            settings.mockpay_decline_rate,
            settings.mockpay_timeout_rate,
        )

    @staticmethod
    def _roll(key: str, salt: str) -> float:
        digest = hashlib.sha256(f"{salt}:{key}".encode()).digest()
        return int.from_bytes(digest[:4], "big") / 2**32

    def _decide(self, key: str) -> Outcome:
        return "declined" if self._roll(key, "decline") < self.decline_rate else "succeeded"

    def charge(self, key: str, amount: int) -> Outcome:
        """Charge once per key. Raises ProviderTimeout when the answer is 'lost' in transit."""
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        with self.pool.connection() as conn:
            row = one(
                conn,
                "INSERT INTO mock_charges (provider_key, amount, outcome) VALUES (%s, %s, %s)"
                " ON CONFLICT (provider_key) DO UPDATE SET provider_key = EXCLUDED.provider_key"
                " RETURNING outcome",
                (key, amount, self._decide(key)),
            )
        assert row is not None
        if self._roll(key, "timeout") < self.timeout_rate:
            raise ProviderTimeout(key)
        outcome: Outcome = row["outcome"]
        return outcome

    def lookup(self, key: str) -> Outcome | None:
        """What happened to this charge? None means the provider never saw it."""
        with self.pool.connection() as conn:
            row = one(conn, "SELECT outcome FROM mock_charges WHERE provider_key = %s", (key,))
        if row is None:
            return None
        outcome: Outcome = row["outcome"]
        return outcome

    def refund(self, key: str) -> None:
        """Give the money back. Safe to call more than once."""
        with self.pool.connection() as conn:
            conn.execute("UPDATE mock_charges SET refunded = true WHERE provider_key = %s", (key,))
