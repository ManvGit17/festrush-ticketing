"""The payment provider contract, run against the in-process mock and the HTTP service alike, plus
what the app does when the service is slow, lossy or down."""

import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import uvicorn

from festrush import invariants, inventory, orders, worker
from festrush.mockpay import MockProvider, ProviderTimeout
from festrush.payments_service import (
    HttpProvider,
    Provider,
    ProviderUnavailable,
    create_payments_app,
)
from tests.conftest import Db, make_settings


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Service:
    """The payments service running in a thread, so a test can stop and restart it."""

    def __init__(self, port: int, **rates: Any) -> None:
        self.port = port
        self.settings = make_settings(
            mockpay_decline_rate=rates.get("decline_rate", 0.0),
            mockpay_timeout_rate=rates.get("timeout_rate", 0.0),
            mockpay_latency_ms=rates.get("latency_ms", 0),
        )
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        config = uvicorn.Config(
            create_payments_app(self.settings), host="127.0.0.1", port=self.port, log_level="error"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started

    def stop(self) -> None:
        if self.server and self.thread:
            self.server.should_exit = True
            self.thread.join(timeout=10)
            self.server = self.thread = None


@pytest.fixture
def service() -> Iterator[Callable[..., Service]]:
    started: list[Service] = []

    def make(**rates: Any) -> Service:
        svc = Service(free_port(), **rates)
        svc.start()
        started.append(svc)
        return svc

    yield make
    for svc in started:
        svc.stop()


@pytest.fixture(params=["in-process", "http"])
def make_provider(
    request: pytest.FixtureRequest, app_state: Any, service: Callable[..., Service]
) -> Callable[..., Provider]:
    """Builds a provider with the given failure rates, in-process or behind the HTTP service."""

    def build(**rates: Any) -> Provider:
        if request.param == "in-process":
            return MockProvider(app_state.pool, **rates)
        return HttpProvider(service(**rates).url)

    return build


# --- The contract: both implementations must behave the same --------------------------------------


def test_a_charge_is_taken_once_per_key(make_provider: Callable[..., Provider], db: Db) -> None:
    provider = make_provider()
    assert provider.charge("k1", 500) == "succeeded"
    assert provider.charge("k1", 500) == "succeeded"
    row = db.execute("SELECT count(*) AS n FROM mock_charges").fetchone()
    assert row
    assert row["n"] == 1


def test_a_declined_charge_stays_declined(make_provider: Callable[..., Provider]) -> None:
    provider = make_provider(decline_rate=1.0)
    assert provider.charge("k1", 500) == "declined"
    assert provider.charge("k1", 500) == "declined"
    assert provider.lookup("k1") == "declined"


def test_a_lost_answer_raises_but_the_charge_is_still_on_record(
    make_provider: Callable[..., Provider],
) -> None:
    provider = make_provider(timeout_rate=1.0)
    with pytest.raises(ProviderTimeout):
        provider.charge("k1", 500)
    assert provider.lookup("k1") == "succeeded"  # the money was taken; only the reply was lost


def test_lookup_of_an_unknown_key_is_none(make_provider: Callable[..., Provider]) -> None:
    assert make_provider().lookup("never-charged") is None


def test_refunding_twice_is_harmless(make_provider: Callable[..., Provider], db: Db) -> None:
    provider = make_provider()
    provider.charge("k1", 500)
    provider.refund("k1")
    provider.refund("k1")
    row = db.execute("SELECT refunded FROM mock_charges WHERE provider_key = 'k1'").fetchone()
    assert row
    assert row["refunded"] is True


# --- Only the HTTP client: what the network adds ------------------------------------------------------


def test_an_unreachable_service_looks_like_a_lost_answer_for_a_charge() -> None:
    gone = HttpProvider(f"http://127.0.0.1:{free_port()}", timeout=0.5)
    with pytest.raises(ProviderTimeout):
        gone.charge("k1", 500)  # we cannot know whether it was taken
    with pytest.raises(ProviderUnavailable):
        gone.lookup("k1")
    with pytest.raises(ProviderUnavailable):
        gone.refund("k1")


def test_a_slow_service_times_out_the_client(service: Callable[..., Service]) -> None:
    slow = HttpProvider(service(latency_ms=1500).url, timeout=0.3)
    with pytest.raises(ProviderTimeout):
        slow.charge("k1", 500)


# --- The app with the provider as a separate service ----------------------------------------------------


def hold_and_order(app_state: Any, make_event: Any, make_user: Any) -> int:
    user, event_id = make_user("asha"), make_event("Hackathon", 500, 3)
    with app_state.pool.connection() as conn:
        inventory.reserve(conn, app_state.settings, user, event_id)
        ref = orders.create_order(conn, user)
    assert ref.id is not None
    return ref.id


def test_paying_through_the_service_confirms_the_order(
    app_state: Any, make_event: Any, make_user: Any, service: Callable[..., Service], db: Db
) -> None:
    order_id = hold_and_order(app_state, make_event, make_user)
    provider = HttpProvider(service().url)
    assert orders.run_payment(app_state.pool, provider, order_id) == "succeeded"
    worker.process_outbox(app_state.pool, app_state.settings)
    assert invariants.violations(db) == {}


def test_a_service_that_is_down_during_payment_never_double_charges_or_loses_the_seat(
    app_state: Any, make_event: Any, make_user: Any, service: Callable[..., Service], db: Db
) -> None:
    """The provider dies, comes back, and the order is settled by the worker, not left hanging."""
    order_id = hold_and_order(app_state, make_event, make_user)
    svc = service()
    provider = HttpProvider(svc.url, timeout=0.5)
    svc.stop()  # the provider is down when the student pays
    assert orders.run_payment(app_state.pool, provider, order_id) == "unknown"

    quick = make_settings(payment_reconcile_seconds=0)
    # Still down: the reconciler cannot tell, so it leaves the payment alone.
    assert orders.reconcile_payments(app_state.pool, provider, quick) == 0
    status = db.execute("SELECT status FROM payments").fetchone()
    assert status
    assert status["status"] == "unknown"

    svc.start()  # same port: it is back
    # It never saw the charge, so nothing was taken: the payment is failed and the hold is kept.
    assert orders.reconcile_payments(app_state.pool, provider, quick) == 1
    order = db.execute("SELECT status FROM orders").fetchone()
    assert order
    assert order["status"] == "payment_failed"
    held = db.execute("SELECT status FROM reservations").fetchone()
    assert held
    assert held["status"] == "held"
    assert invariants.violations(db) == {}
