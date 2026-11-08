"""The background worker: releases expired holds, relays the outbox, reconciles payments.

Anything here can be killed at any moment and started again without losing or repeating work:

* The outbox relay claims rows with ``FOR UPDATE SKIP LOCKED`` and writes the result and the
  'processed' mark in one transaction, so a killed worker leaves its rows pending and the next
  one picks them up. Handlers are idempotent too (one mailbox row per order and kind), so even
  a repeated delivery sends a confirmation once.
* Hold expiry and payment reconciliation are idempotent by construction (state machines).
"""

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any

from festrush import idempotency, inventory, orders
from festrush.config import Settings
from festrush.db import Conn, Pool
from festrush.mockpay import MockProvider

log = logging.getLogger(__name__)


def _confirmation(conn: Conn, payload: dict[str, Any]) -> None:
    order = orders.get_order(conn, int(payload["order_id"]))
    assert order is not None
    items = ", ".join(i["name"] for i in order["items"])
    conn.execute(
        "INSERT INTO mailbox (order_id, user_id, kind, subject, body) VALUES (%s, %s, 'confirmation', %s, %s)"
        " ON CONFLICT (order_id, kind) DO NOTHING",
        (
            order["id"],
            order["user_id"],
            "Your FestRush tickets are confirmed",
            f"Order {order['id']}: {items}. Total Rs {order['total']}.",
        ),
    )


def _refund(conn: Conn, payload: dict[str, Any]) -> None:
    order = orders.get_order(conn, int(payload["order_id"]))
    assert order is not None
    conn.execute(
        "INSERT INTO mailbox (order_id, user_id, kind, subject, body) VALUES (%s, %s, 'refund', %s, %s)"
        " ON CONFLICT (order_id, kind) DO NOTHING",
        (
            order["id"],
            order["user_id"],
            "Your FestRush payment was refunded",
            f"Order {order['id']} expired before payment completed, so Rs {order['total']} was returned.",
        ),
    )


HANDLERS: dict[str, Callable[[Conn, dict[str, Any]], None]] = {
    "order.confirmed": _confirmation,
    "order.refunded": _refund,
}


def process_outbox(pool: Pool, settings: Settings, limit: int = 50) -> int:
    """Deliver pending outbox events. Returns how many were handled (successfully or not)."""
    handled = 0
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, topic, payload, attempts FROM outbox"
            " WHERE processed_at IS NULL AND dead_at IS NULL AND available_at <= now()"
            " ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        ).fetchall()
        for row in rows:
            handled += 1
            try:
                with conn.transaction():  # a savepoint: one bad event doesn't undo the batch
                    HANDLERS[row["topic"]](conn, row["payload"])
                    if settings.worker_handler_delay_ms:
                        time.sleep(settings.worker_handler_delay_ms / 1000)
                    conn.execute("UPDATE outbox SET processed_at = now() WHERE id = %s", (row["id"],))
            except Exception as exc:
                attempts = int(row["attempts"]) + 1
                dead = attempts >= settings.outbox_max_attempts
                backoff = min(2**attempts, 300) * random.uniform(0.5, 1.5)  # noqa: S311
                log.warning("outbox event %s failed (attempt %s): %s", row["id"], attempts, exc)
                conn.execute(
                    "UPDATE outbox SET attempts = %s, last_error = %s,"
                    " available_at = now() + make_interval(secs => %s),"
                    " dead_at = CASE WHEN %s THEN now() END WHERE id = %s",
                    (attempts, str(exc)[:500], backoff, dead, row["id"]),
                )
    return handled


def run_once(pool: Pool, settings: Settings, provider: MockProvider) -> int:
    """One pass of every job. Returns the amount of work found, so callers can idle on zero."""
    work = orders.expire_stale(pool, settings)
    work += process_outbox(pool, settings)
    work += orders.reconcile_payments(pool, provider, settings)
    return work


def run_forever(
    pool: Pool, settings: Settings, provider: MockProvider, stop: threading.Event | None = None
) -> None:
    stop = stop or threading.Event()
    last_prune = 0.0
    while not stop.is_set():
        try:
            work = run_once(pool, settings, provider)
            if time.monotonic() - last_prune > 300:
                with pool.connection() as conn:
                    idempotency.prune(conn)
                last_prune = time.monotonic()
        except Exception:
            log.exception("worker pass failed; retrying")
            work = 0
        if work == 0:
            stop.wait(settings.worker_poll_ms / 1000)


__all__ = ["inventory", "process_outbox", "run_forever", "run_once"]
