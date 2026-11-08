"""Orders and payments.

An order is created from a user's live holds. Paying for it goes:

    create_order        pending order, one payment row ('initiated'); holds attached
    run_payment         exactly one caller wins the right to call the provider ('processing')
    apply_payment_outcome  idempotent: the provider's answer, however and however often it arrives

States are enforced by triggers in the database (migration 0002), so these functions cannot write
an illegal move. Lock order everywhere is: order row, then reservation rows.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from festrush import inventory, metrics
from festrush.config import Settings
from festrush.db import Conn, Pool, one
from festrush.mockpay import Outcome, ProviderTimeout
from festrush.payments_service import Provider, ProviderUnavailable
from festrush.tracing import tracer

log = logging.getLogger(__name__)

SETTLED = ("succeeded", "failed", "refunded")


@dataclass(frozen=True)
class OrderRef:
    id: int | None  # None when there was nothing to order
    status: str | None = None


def emit(conn: Conn, topic: str, payload: dict[str, Any]) -> None:
    """Queue an event in the outbox. Same transaction as the change it announces."""
    conn.execute(
        "INSERT INTO outbox (topic, payload) VALUES (%s, %s::jsonb)",
        (topic, json.dumps(payload)),
    )


def create_order(conn: Conn, user_id: int) -> OrderRef:
    """Turn the user's live, unattached holds into a pending order with a payment to make."""
    holds = conn.execute(
        "SELECT r.id, e.fee FROM reservations r JOIN events e ON e.id = r.event_id"
        " WHERE r.user_id = %s AND r.status = 'held' AND r.order_id IS NULL AND r.expires_at > now()"
        " ORDER BY r.id FOR UPDATE OF r",
        (user_id,),
    ).fetchall()
    if not holds:
        return OrderRef(None)
    total = sum(int(h["fee"]) for h in holds)
    order = one(
        conn,
        "INSERT INTO orders (user_id, total) VALUES (%s, %s) RETURNING id, status",
        (user_id, total),
    )
    assert order is not None
    conn.execute(
        "UPDATE reservations SET order_id = %s WHERE id = ANY(%s)",
        (order["id"], [int(h["id"]) for h in holds]),
    )
    conn.execute(
        "INSERT INTO payments (order_id, provider_key, amount) VALUES (%s, %s, %s)",
        (order["id"], f"pay-{order['id']}", total),
    )
    return OrderRef(int(order["id"]), order["status"])


def get_order(conn: Conn, order_id: int, user_id: int | None = None) -> dict[str, Any] | None:
    """An order with its payment status and items. Pass user_id to restrict to its owner."""
    order = one(
        conn,
        "SELECT o.id, o.user_id, o.status, o.total, p.status AS payment_status"
        " FROM orders o LEFT JOIN payments p ON p.order_id = o.id"
        " WHERE o.id = %s AND (%s::bigint IS NULL OR o.user_id = %s)",
        (order_id, user_id, user_id),
    )
    if order is None:
        return None
    order["items"] = conn.execute(
        "SELECT r.id AS reservation_id, e.id AS event_id, e.name, e.fee, r.status"
        " FROM reservations r JOIN events e ON e.id = r.event_id WHERE r.order_id = %s ORDER BY r.id",
        (order_id,),
    ).fetchall()
    return order


Notifier = Callable[[Conn, dict[str, Any]], None]


def run_payment(pool: Pool, provider: Provider, order_id: int, notify: Notifier | None = None) -> str:
    """Charge the order. Safe to call any number of times, concurrently.

    Only the caller that flips the payment from 'initiated' to 'processing' talks to the provider;
    everyone else just gets the current status. The provider is called with no database
    transaction open, so a slow provider never holds a lock. Returns the payment status.
    """
    with pool.connection() as conn:
        won = one(
            conn,
            "UPDATE payments SET status = 'processing', updated_at = now()"
            " WHERE order_id = %s AND status = 'initiated' RETURNING id, provider_key, amount",
            (order_id,),
        )
        if won is None:
            current = one(conn, "SELECT status FROM payments WHERE order_id = %s", (order_id,))
            return str(current["status"]) if current else "missing"

    try:
        with tracer.start_as_current_span("payments.charge"):
            outcome = provider.charge(won["provider_key"], won["amount"])
    except ProviderTimeout:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE payments SET status = 'unknown', updated_at = now()"
                " WHERE id = %s AND status = 'processing'",
                (won["id"],),
            )
        return "unknown"
    with pool.connection() as conn:
        return apply_payment_outcome(conn, provider, int(won["id"]), outcome, notify)


def apply_payment_outcome(
    conn: Conn,
    provider: Provider,
    payment_id: int,
    outcome: str | None,
    notify: Notifier | None = None,
) -> str:
    """Record what the provider says happened. Idempotent.

    ``outcome`` is 'succeeded', 'declined', or None when the provider has no record of the charge
    (it was never made, so nothing was taken). Returns the payment's status afterwards.
    """
    payment = one(
        conn, "SELECT id, order_id, provider_key, status FROM payments WHERE id = %s", (payment_id,)
    )
    assert payment is not None
    order = one(
        conn,
        "SELECT id, user_id, status, total FROM orders WHERE id = %s FOR UPDATE",
        (payment["order_id"],),
    )
    assert order is not None
    # Re-read under the order lock: another caller may have settled it while we waited.
    payment = one(conn, "SELECT id, provider_key, status FROM payments WHERE id = %s", (payment_id,))
    assert payment is not None
    if payment["status"] in SETTLED:
        return str(payment["status"])

    if outcome in (None, "declined"):
        conn.execute("UPDATE payments SET status = 'failed', updated_at = now() WHERE id = %s", (payment_id,))
        if order["status"] == "pending":
            conn.execute("UPDATE orders SET status = 'payment_failed' WHERE id = %s", (order["id"],))
            # The seats go back to being plain holds, so the student can try another order.
            conn.execute("UPDATE reservations SET order_id = NULL WHERE order_id = %s", (order["id"],))
        metrics.PAYMENTS.labels("failed").inc()
        return "failed"

    # The provider took the money.
    held = conn.execute(
        "SELECT id FROM reservations WHERE order_id = %s AND status = 'held' FOR UPDATE",
        (order["id"],),
    ).fetchall()
    if order["status"] == "pending" and held:
        conn.execute(
            "UPDATE payments SET status = 'succeeded', updated_at = now() WHERE id = %s",
            (payment_id,),
        )
        conn.execute("UPDATE orders SET status = 'paid' WHERE id = %s", (order["id"],))
        conn.execute(
            "UPDATE reservations SET status = 'confirmed' WHERE id = ANY(%s)",
            ([int(h["id"]) for h in held],),
        )
        conn.execute("UPDATE orders SET status = 'confirmed' WHERE id = %s", (order["id"],))
        payload = {"order_id": int(order["id"]), "user_id": int(order["user_id"])}
        if notify is not None:
            notify(conn, payload)  # inline: the student waits for the message and the order row stays locked
        else:
            emit(conn, "order.confirmed", payload)
        metrics.PAYMENTS.labels("succeeded").inc()
        return "succeeded"

    # The money arrived but the seats are gone (the order expired first): give it back.
    conn.execute("UPDATE payments SET status = 'refunded', updated_at = now() WHERE id = %s", (payment_id,))
    if order["status"] == "expired":
        conn.execute("UPDATE orders SET status = 'refunded' WHERE id = %s", (order["id"],))
    provider.refund(payment["provider_key"])
    emit(conn, "order.refunded", {"order_id": int(order["id"]), "user_id": int(order["user_id"])})
    metrics.PAYMENTS.labels("refunded").inc()
    return "refunded"


def _retry_unseen_charge(pool: Pool, provider: Provider, payment: dict[str, Any]) -> Outcome | None:
    """The provider has no record of this charge, so nothing was taken: it was never sent (the web
    process died or the provider was down). If the order can still be fulfilled, send it again.
    The key is the same, so the provider charges at most once. Returns the outcome, or None to
    give up on it (the order is no longer pending)."""
    with pool.connection() as conn:
        order = one(
            conn,
            "SELECT o.status, p.amount FROM payments p JOIN orders o ON o.id = p.order_id WHERE p.id = %s",
            (payment["id"],),
        )
    if order is None or order["status"] != "pending":
        return None
    return provider.charge(payment["provider_key"], int(order["amount"]))


def reconcile_payments(pool: Pool, provider: Provider, settings: Settings, limit: int = 50) -> int:
    """Ask the provider about payments that have sat unsettled too long, and apply the answer.

    Covers a lost response (timeout) and a web process that died between taking the right to
    charge and hearing back. Returns how many payments were settled.
    """
    with pool.connection() as conn:
        stuck = conn.execute(
            "SELECT id, provider_key FROM payments WHERE status IN ('processing', 'unknown')"
            " AND updated_at < now() - make_interval(secs => %s) ORDER BY updated_at LIMIT %s",
            (settings.payment_reconcile_seconds, limit),
        ).fetchall()
    settled = 0
    for payment in stuck:
        try:
            outcome = provider.lookup(payment["provider_key"])
            if outcome is None:
                outcome = _retry_unseen_charge(pool, provider, payment)
        except (ProviderUnavailable, ProviderTimeout):
            log.warning("payment provider unreachable; will ask again later")
            continue
        with pool.connection() as conn:
            if apply_payment_outcome(conn, provider, int(payment["id"]), outcome) in SETTLED:
                settled += 1
    return settled


def expire_stale(pool: Pool, settings: Settings) -> int:
    with pool.connection() as conn:
        expired = inventory.expire_holds(conn, settings)
    metrics.HOLDS_EXPIRED.inc(expired)
    return expired
