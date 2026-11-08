"""Prometheus metrics and tracing set-up."""

import subprocess
import sys
import textwrap
from typing import Any

from fastapi.testclient import TestClient

from festrush import inventory, orders, worker
from festrush.config import REPO_ROOT
from festrush.mockpay import MockProvider
from tests.conftest import Db, make_settings


def scrape(client: TestClient) -> str:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    return r.text


def value(text: str, name: str, labels: str = "") -> float:
    for line in text.splitlines():
        if line.startswith(name) and labels in line and not line.startswith("#"):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{name}{labels} not found")


def test_requests_are_timed_by_route_template_not_by_url(client: TestClient, make_event: Any) -> None:
    event_id = make_event()
    for _ in range(3):
        client.get(f"/api/events/{event_id}")
    text = scrape(client)
    assert 'route="/api/events/{event_id}"' in text
    assert f'/api/events/{event_id}"' not in text  # a high-cardinality label would break Prometheus
    assert value(text, "festrush_http_request_duration_seconds_count", 'route="/api/events/{event_id}"') >= 3


def test_the_metrics_endpoint_does_not_measure_itself(client: TestClient) -> None:
    scrape(client)
    assert 'route="/metrics"' not in scrape(client)


def test_seat_claims_and_payments_are_counted(
    client: TestClient, make_event: Any, make_user: Any, app_state: Any
) -> None:
    event_id, user = make_event(capacity=1), make_user("asha")
    before = scrape(client)
    with app_state.pool.connection() as conn:
        inventory.reserve(conn, app_state.settings, user, event_id)
        ref = orders.create_order(conn, user)
    assert ref.id is not None
    orders.run_payment(app_state.pool, MockProvider(app_state.pool), ref.id)
    worker.process_outbox(app_state.pool, app_state.settings)
    after = scrape(client)

    def grew(name: str, labels: str) -> float:
        try:
            return value(after, name, labels) - value(before, name, labels)
        except AssertionError:
            return value(after, name, labels)

    assert grew("festrush_reservations_total", 'outcome="held"') == 1
    assert grew("festrush_payments_total", 'result="succeeded"') == 1
    assert grew("festrush_outbox_processed_total", 'topic="order.confirmed"') == 1


def test_database_gauges_describe_shared_state(
    client: TestClient, make_event: Any, make_user: Any, app_state: Any, db: Db
) -> None:
    event_id, user = make_event(capacity=2), make_user("asha")
    with app_state.pool.connection() as conn:
        inventory.reserve(conn, app_state.settings, user, event_id)
    text = scrape(client)
    assert value(text, "festrush_holds_active") == 1
    assert value(text, "festrush_seats_confirmed") == 0
    assert value(text, "festrush_outbox_pending") == 0
    assert value(text, "festrush_payments_unsettled") == 0
    assert "festrush_db_lock_waits" in text
    db.execute("INSERT INTO outbox (topic, payload) VALUES ('order.confirmed', '{}')")
    assert value(scrape(client), "festrush_outbox_pending") == 1


def test_tracing_is_off_without_an_endpoint() -> None:
    from festrush import tracing

    assert tracing.setup(make_settings(otlp_endpoint=None), "test") is False


def test_trace_context_reaches_the_payments_service_headers() -> None:
    """With tracing on, a call to the provider carries the W3C traceparent header. Runs in its own
    process because an OpenTelemetry tracer provider can only be installed once."""
    script = textwrap.dedent(
        """
        from festrush import tracing
        from festrush.config import Settings
        from festrush.payments_service import _trace_headers

        settings = Settings(_env_file=None, secret_key="x" * 20, otlp_endpoint="http://127.0.0.1:9")
        assert tracing.setup(settings, "test") is True
        with tracing.tracer.start_as_current_span("parent"):
            headers = _trace_headers({})
        assert "traceparent" in headers, headers
        print("ok")
        """
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("ok")
