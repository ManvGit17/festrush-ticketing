"""Prometheus metrics: what the system is doing, in numbers a dashboard can draw.

Two kinds:

* **Counters and histograms** updated as things happen (requests, seat claims, payments). With
  several web processes each keeps its own and ``/metrics`` merges them (prometheus_client's
  multiprocess mode, enabled by ``PROMETHEUS_MULTIPROC_DIR``, which ``festrush serve`` sets).
* **Gauges read from the database when scraped** (outbox backlog, queue size, active holds,
  lock waits). They describe shared state, so every process would otherwise report its own view.
"""

import os
from collections.abc import Iterable

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest, multiprocess
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from festrush.db import Conn

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

REQUEST_SECONDS = Histogram(
    "festrush_http_request_duration_seconds",
    "Time to answer a web request",
    ["route", "method", "status"],
    buckets=BUCKETS,
)
RESERVATIONS = Counter(
    "festrush_reservations_total", "Seat claims by strategy and outcome", ["strategy", "outcome"]
)
PAYMENTS = Counter("festrush_payments_total", "Payment results as applied to orders", ["result"])
OUTBOX_PROCESSED = Counter("festrush_outbox_processed_total", "Outbox events delivered", ["topic"])
OUTBOX_FAILED = Counter("festrush_outbox_failed_total", "Outbox deliveries that raised", ["topic"])
HOLDS_EXPIRED = Counter("festrush_holds_expired_total", "Holds released because time ran out")
ADMITTED = Counter("festrush_queue_admitted_total", "Students admitted from the waiting room")

_GAUGE_SQL = {
    "festrush_outbox_pending": (
        "Outbox events not yet delivered",
        "SELECT count(*) FROM outbox WHERE processed_at IS NULL AND dead_at IS NULL",
    ),
    "festrush_outbox_oldest_age_seconds": (
        "Age of the oldest undelivered outbox event",
        "SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0)::float FROM outbox"
        " WHERE processed_at IS NULL AND dead_at IS NULL",
    ),
    "festrush_outbox_dead": (
        "Outbox events given up on",
        "SELECT count(*) FROM outbox WHERE dead_at IS NOT NULL",
    ),
    "festrush_queue_waiting": (
        "Students waiting in the waiting room",
        "SELECT count(*) FROM queue_entries WHERE admitted_at IS NULL",
    ),
    "festrush_holds_active": (
        "Seats currently on hold",
        "SELECT count(*) FROM reservations WHERE status = 'held'",
    ),
    "festrush_seats_confirmed": (
        "Seats confirmed (paid)",
        "SELECT count(*) FROM reservations WHERE status = 'confirmed'",
    ),
    "festrush_payments_unsettled": (
        "Payments waiting on the provider",
        "SELECT count(*) FROM payments WHERE status IN ('initiated', 'processing', 'unknown')",
    ),
    "festrush_db_lock_waits": (
        "Database sessions currently waiting on a lock",
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE datname = current_database() AND wait_event_type = 'Lock'",
    ),
    "festrush_db_sessions": (
        "Database sessions in this database",
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()",
    ),
}


class DatabaseGauges(Collector):
    """Reads shared state from PostgreSQL each time Prometheus scrapes."""

    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def collect(self) -> Iterable[Metric]:
        for name, (help_text, query) in _GAUGE_SQL.items():
            row = self.conn.execute(query).fetchone()
            value = float(next(iter(row.values()))) if row else 0.0
            gauge = GaugeMetricFamily(name, help_text)
            gauge.add_metric([], value)
            yield gauge


def render(conn: Conn) -> bytes:
    """The text Prometheus scrapes: merged process metrics plus the database gauges."""
    registry = CollectorRegistry()
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    else:  # a single process (tests, `festrush serve` without workers)
        from prometheus_client import REGISTRY

        registry = REGISTRY
    gauges = DatabaseGauges(conn)
    registry.register(gauges)
    try:
        return generate_latest(registry)
    finally:
        registry.unregister(gauges)
