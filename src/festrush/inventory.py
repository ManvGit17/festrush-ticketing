"""Claiming seats: four strategies behind one function, plus releasing and expiring holds.

Every strategy answers the same question, "may this user hold one more seat at this event?",
and every one inserts the same ``reservations`` row when the answer is yes. They differ only in
how they stop two requests from taking the last seat at once:

* ``naive``          reads the number of active reservations, compares, then inserts. Two
                     requests can both read "one seat left". This is the baseline, and it is
                     meant to oversell under load.
* ``row_lock``       locks the event row (``SELECT ... FOR UPDATE``), then counts and inserts,
                     so claims for one event run one at a time.
* ``atomic_update``  one ``UPDATE events SET claimed = claimed + 1 WHERE claimed < capacity``.
                     The database decides in a single statement.
* ``skip_locked``    takes a free row from ``seats`` with ``FOR UPDATE SKIP LOCKED``, so
                     concurrent buyers take different rows instead of waiting for one.

The derived state each strategy relies on (``events.claimed``, ``seats.reservation_id``) is
rebuilt from the reservations by ``rebuild_inventory()`` when the strategy is switched.
"""

import enum
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg.errors

from festrush.config import Settings
from festrush.db import Conn, one

STRATEGIES = ("naive", "row_lock", "atomic_update", "skip_locked")


class Outcome(enum.Enum):
    HELD = "held"
    ALREADY = "already"  # this user already holds or owns a seat at this event
    FULL = "full"
    BUSY = "busy"  # skip_locked only: free seats exist but are locked by in-flight claims
    NO_EVENT = "no_event"


@dataclass(frozen=True)
class Reserved:
    outcome: Outcome
    reservation_id: int | None = None
    expires_at: datetime | None = None


class _Rollback(Exception):
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome


_INSERT = (
    "INSERT INTO reservations (user_id, event_id, status, expires_at)"
    " VALUES (%s, %s, 'held', now() + %s)"
    " ON CONFLICT (user_id, event_id) WHERE status IN ('held', 'confirmed') DO NOTHING"
    " RETURNING id, expires_at"
)


def _insert(conn: Conn, user_id: int, event_id: int, hold: timedelta) -> dict[str, Any] | None:
    return one(conn, _INSERT, (user_id, event_id, hold))


# --- The four strategies. Each runs inside the transaction opened by reserve() ----------------


def _naive(conn: Conn, user_id: int, event_id: int, hold: timedelta, delay_ms: int) -> Reserved:
    row = one(
        conn,
        "SELECT e.capacity, (SELECT count(*) FROM reservations r"
        " WHERE r.event_id = e.id AND r.status IN ('held', 'confirmed')) AS taken"
        " FROM events e WHERE e.id = %s",
        (event_id,),
    )
    if row is None:
        return Reserved(Outcome.NO_EVENT)
    if delay_ms:
        time.sleep(delay_ms / 1000)  # the window in which another request can read the same count
    if row["taken"] >= row["capacity"]:
        return Reserved(Outcome.FULL)
    made = _insert(conn, user_id, event_id, hold)
    if made is None:
        return Reserved(Outcome.ALREADY)
    return Reserved(Outcome.HELD, made["id"], made["expires_at"])


def _row_lock(conn: Conn, user_id: int, event_id: int, hold: timedelta) -> Reserved:
    row = one(conn, "SELECT capacity FROM events WHERE id = %s FOR UPDATE", (event_id,))
    if row is None:
        return Reserved(Outcome.NO_EVENT)
    taken = one(
        conn,
        "SELECT count(*) AS n FROM reservations WHERE event_id = %s AND status IN ('held', 'confirmed')",
        (event_id,),
    )
    if taken is not None and taken["n"] >= row["capacity"]:
        return Reserved(Outcome.FULL)
    made = _insert(conn, user_id, event_id, hold)
    if made is None:
        return Reserved(Outcome.ALREADY)
    return Reserved(Outcome.HELD, made["id"], made["expires_at"])


def _atomic_update(conn: Conn, user_id: int, event_id: int, hold: timedelta) -> Reserved:
    # Insert first: a duplicate request is refused without touching the shared counter.
    made = _insert(conn, user_id, event_id, hold)
    if made is None:
        return Reserved(Outcome.ALREADY)  # a missing event raises ForeignKeyViolation instead
    claimed = one(
        conn,
        "UPDATE events SET claimed = claimed + 1 WHERE id = %s AND claimed < capacity RETURNING id",
        (event_id,),
    )
    if claimed is None:
        raise _Rollback(Outcome.FULL)  # undoes the reservation row
    return Reserved(Outcome.HELD, made["id"], made["expires_at"])


def _skip_locked(conn: Conn, user_id: int, event_id: int, hold: timedelta) -> Reserved:
    made = _insert(conn, user_id, event_id, hold)
    if made is None:
        return Reserved(Outcome.ALREADY)  # a missing event raises ForeignKeyViolation instead
    for attempt in range(4):
        seat = one(
            conn,
            "SELECT id FROM seats WHERE event_id = %s AND reservation_id IS NULL"
            " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
            (event_id,),
        )
        if seat is not None:
            conn.execute("UPDATE seats SET reservation_id = %s WHERE id = %s", (made["id"], seat["id"]))
            return Reserved(Outcome.HELD, made["id"], made["expires_at"])
        # No unlocked free seat. If free seats exist they are locked by claims still in flight,
        # which may yet roll back, so wait a moment and look again before saying "sold out".
        free = one(
            conn,
            "SELECT 1 AS x FROM seats WHERE event_id = %s AND reservation_id IS NULL LIMIT 1",
            (event_id,),
        )
        if free is None:
            raise _Rollback(Outcome.FULL)
        if attempt < 3:
            time.sleep(0.005 * (attempt + 1))
    raise _Rollback(Outcome.BUSY)


def reserve(conn: Conn, settings: Settings, user_id: int, event_id: int) -> Reserved:
    """Hold one seat for a user. Atomic: either the hold exists afterwards, or nothing changed.

    Needs a connection with no statement run on it yet in this transaction, or one inside a
    caller-managed transaction (it then uses a savepoint).
    """
    hold = timedelta(minutes=settings.hold_minutes)
    strategy = settings.inventory_strategy
    try:
        with conn.transaction():
            if strategy == "naive":
                return _naive(conn, user_id, event_id, hold, settings.naive_delay_ms)
            if strategy == "row_lock":
                return _row_lock(conn, user_id, event_id, hold)
            if strategy == "atomic_update":
                return _atomic_update(conn, user_id, event_id, hold)
            return _skip_locked(conn, user_id, event_id, hold)
    except _Rollback as stop:
        return Reserved(stop.outcome)
    except psycopg.errors.ForeignKeyViolation:
        return Reserved(Outcome.NO_EVENT)


# --- Giving seats back ------------------------------------------------------------------------


def release_inventory(conn: Conn, strategy: str, reservation_ids: list[int]) -> None:
    """Undo the strategy's bookkeeping for reservations that just stopped being active."""
    if not reservation_ids:
        return
    if strategy == "skip_locked":
        conn.execute(
            "UPDATE seats SET reservation_id = NULL WHERE reservation_id = ANY(%s)",
            (reservation_ids,),
        )
    elif strategy == "atomic_update":
        conn.execute(
            "UPDATE events e SET claimed = GREATEST(e.claimed - c.n, 0)"
            " FROM (SELECT event_id, count(*) AS n FROM reservations"
            "       WHERE id = ANY(%s) GROUP BY event_id) c WHERE e.id = c.event_id",
            (reservation_ids,),
        )


def expire_holds(conn: Conn, settings: Settings, limit: int = 500) -> int:
    """Release holds whose time is up. Returns how many were expired.

    A hold attached to an order with a payment in flight gets ``payment_grace_seconds`` extra,
    and is expired together with its order. Lock order is order row, then reservation rows,
    the same as in orders.apply_payment_outcome, so the two never deadlock.
    """
    expired = 0
    # 1. Plain holds: nobody is paying for them.
    rows = conn.execute(
        "SELECT r.id FROM reservations r LEFT JOIN orders o ON o.id = r.order_id"
        " WHERE r.status = 'held' AND r.expires_at < now()"
        "   AND (r.order_id IS NULL OR o.status <> 'pending')"
        " ORDER BY r.expires_at LIMIT %s FOR UPDATE OF r SKIP LOCKED",
        (limit,),
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    if ids:
        conn.execute("UPDATE reservations SET status = 'expired' WHERE id = ANY(%s)", (ids,))
        release_inventory(conn, settings.inventory_strategy, ids)
        expired += len(ids)

    # 2. Holds behind a pending order, once the grace period has passed.
    orders = conn.execute(
        "SELECT o.id FROM orders o WHERE o.status = 'pending' AND EXISTS ("
        "  SELECT 1 FROM reservations r WHERE r.order_id = o.id AND r.status = 'held'"
        "  AND r.expires_at < now() - %s) ORDER BY o.id LIMIT %s FOR UPDATE OF o SKIP LOCKED",
        (timedelta(seconds=settings.payment_grace_seconds), limit),
    ).fetchall()
    for order in orders:
        held = conn.execute(
            "SELECT id FROM reservations WHERE order_id = %s AND status = 'held' FOR UPDATE",
            (order["id"],),
        ).fetchall()
        held_ids = [int(r["id"]) for r in held]
        conn.execute("UPDATE reservations SET status = 'expired' WHERE id = ANY(%s)", (held_ids,))
        release_inventory(conn, settings.inventory_strategy, held_ids)
        conn.execute("UPDATE orders SET status = 'expired' WHERE id = %s", (order["id"],))
        expired += len(held_ids)
    return expired


# --- Reading ----------------------------------------------------------------------------------


def availability(conn: Conn, event_id: int) -> dict[str, Any] | None:
    """Seats left, counted from the reservations (the source of truth, whatever the strategy)."""
    return one(
        conn,
        "SELECT e.id, e.name, e.capacity,"
        " (SELECT count(*) FROM reservations r WHERE r.event_id = e.id AND r.status IN ('held', 'confirmed'))"
        "   AS active,"
        " (SELECT count(*) FROM reservations r WHERE r.event_id = e.id AND r.status = 'confirmed')"
        "   AS confirmed FROM events e WHERE e.id = %s",
        (event_id,),
    )
