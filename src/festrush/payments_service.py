"""The payment provider as its own service, and the client the app uses to reach it.

Extracted from the monolith because it is the one part that is external by nature: a real
provider is a different process with its own latency, its own failures and its own outages.
Running the mock the same way makes the app prove it copes (timeouts, retries, reconciliation).
The measurements behind this decision are in docs/architecture.md (experiments E2).

Protocol (JSON over HTTP, no authentication: it listens only on the internal network):

    POST /charges                {"key": str, "amount": int} -> 200 {"outcome": "succeeded"|"declined"}
                                 504 when the charge was taken or declined but the answer is "lost"
    GET  /charges/{key}          -> 200 {"outcome": ...} | 404 if the provider never saw the key
    POST /charges/{key}/refund   -> 200 {"refunded": true}
"""

import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from festrush import tracing
from festrush.config import Settings
from festrush.db import Pool, make_pool
from festrush.mockpay import MockProvider, Outcome, ProviderTimeout


class Provider(Protocol):
    """What the app needs from a payment provider. MockProvider and HttpProvider both fit."""

    def charge(self, key: str, amount: int) -> Outcome:
        """Charge once per key; ProviderTimeout when the answer is lost."""

    def lookup(self, key: str) -> Outcome | None:
        """What happened to this charge? None when the provider never saw the key."""

    def refund(self, key: str) -> None:
        """Give the money back. Safe to repeat."""


def _trace_headers(headers: dict[str, str]) -> dict[str, str]:
    """Add the W3C trace context so the provider's spans join the caller's trace."""
    from opentelemetry.propagate import inject

    inject(headers)
    return headers


class ProviderUnavailable(Exception):
    """The provider could not be reached. Nothing is known about the charge."""


class ChargeIn(BaseModel):
    key: str = Field(min_length=1, max_length=255)
    amount: int = Field(ge=0)


def create_payments_app(settings: Settings, pool: Pool | None = None) -> FastAPI:
    """The provider service. ``pool`` is for tests; normally the service opens its own."""
    holder: dict[str, MockProvider] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        own = pool is None
        p = pool or make_pool(settings.database_url, 1, settings.pool_max_size)
        holder["provider"] = MockProvider.from_settings(p, settings)
        try:
            yield
        finally:
            if own:
                p.close()

    app = FastAPI(title="FestRush payments (mock provider)", docs_url=None, redoc_url=None, lifespan=lifespan)
    tracing.setup(settings, "festrush-payments", app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/charges")
    def charge(body: ChargeIn) -> dict[str, str]:
        try:
            return {"outcome": holder["provider"].charge(body.key, body.amount)}
        except ProviderTimeout as exc:
            raise HTTPException(status_code=504, detail="gateway timeout") from exc

    @app.get("/charges/{key}")
    def lookup(key: str) -> dict[str, str]:
        outcome = holder["provider"].lookup(key)
        if outcome is None:
            raise HTTPException(status_code=404, detail="unknown charge")
        return {"outcome": outcome}

    @app.post("/charges/{key}/refund")
    def refund(key: str) -> dict[str, bool]:
        holder["provider"].refund(key)
        return {"refunded": True}

    return app


class HttpProvider:
    """Talks to the payments service. Same behaviour as MockProvider from the app's point of view,
    plus the failures a network adds: an unreachable service raises ProviderUnavailable (lookup,
    refund) or ProviderTimeout (charge: the charge may or may not have been made)."""

    def __init__(self, base_url: str, timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> tuple[int, dict[str, str]]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(  # noqa: S310 - the URL comes from our own configuration
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=_trace_headers({"Content-Type": "application/json"}),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, {}
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            raise ProviderUnavailable(str(err)) from err

    def charge(self, key: str, amount: int) -> Outcome:
        try:
            status, body = self._call("POST", "/charges", {"key": key, "amount": amount})
        except ProviderUnavailable as exc:
            raise ProviderTimeout(key) from exc  # we do not know whether the charge was made
        if status == 504:
            raise ProviderTimeout(key)
        if status != 200:
            raise ProviderTimeout(key)
        outcome: Outcome = body["outcome"]  # type: ignore[assignment]
        return outcome

    def lookup(self, key: str) -> Outcome | None:
        status, body = self._call("GET", f"/charges/{key}")
        if status == 404:
            return None
        if status != 200:
            raise ProviderUnavailable(f"HTTP {status}")
        outcome: Outcome = body["outcome"]  # type: ignore[assignment]
        return outcome

    def refund(self, key: str) -> None:
        status, _ = self._call("POST", f"/charges/{key}/refund")
        if status != 200:
            raise ProviderUnavailable(f"HTTP {status}")


def make_provider(pool: Pool, settings: Settings) -> Provider:
    """The in-process mock, or the payments service when ``FESTRUSH_PAYMENTS_URL`` is set."""
    if settings.payments_url:
        return HttpProvider(settings.payments_url, settings.payments_timeout_seconds)
    return MockProvider.from_settings(pool, settings)
