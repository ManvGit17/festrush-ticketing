"""The properties that must hold however many requests race, retry or crash.

Every load test ends by running these against the database and failing if any is violated. They
read the reservations, orders and payments themselves, never the counters the strategies keep,
so a strategy cannot pass by agreeing with its own bookkeeping.

Run them when the system is quiet: after the worker has drained the outbox and settled every
payment, since two of them (messages and charges) are about work that is allowed to be in flight.
"""

from dataclasses import dataclass

from festrush.db import Conn


@dataclass(frozen=True)
class Check:
    name: str
    description: str
    sql: str  # returns one row per violation


CHECKS: tuple[Check, ...] = (
    Check(
        "no_oversell",
        "active reservations (held + confirmed) never exceed an event's capacity",
        "SELECT e.id, e.name, e.capacity, count(r.id) AS active FROM events e"
        " JOIN reservations r ON r.event_id = e.id AND r.status IN ('held', 'confirmed')"
        " GROUP BY e.id HAVING count(r.id) > e.capacity",
    ),
    Check(
        "one_seat_per_user_per_event",
        "no user holds or owns two seats at one event",
        "SELECT user_id, event_id, count(*) FROM reservations WHERE status IN ('held', 'confirmed')"
        " GROUP BY user_id, event_id HAVING count(*) > 1",
    ),
    Check(
        "confirmed_seats_are_paid",
        "every confirmed reservation belongs to a confirmed order with a succeeded payment",
        "SELECT r.id FROM reservations r LEFT JOIN orders o ON o.id = r.order_id"
        " LEFT JOIN payments p ON p.order_id = o.id"
        " WHERE r.status = 'confirmed'"
        "   AND (o.status IS DISTINCT FROM 'confirmed' OR p.status IS DISTINCT FROM 'succeeded')",
    ),
    Check(
        "paid_orders_have_seats",
        "every confirmed order has at least one confirmed reservation and none that are not",
        "SELECT o.id FROM orders o WHERE o.status = 'confirmed' AND ("
        "  NOT EXISTS (SELECT 1 FROM reservations r WHERE r.order_id = o.id AND r.status = 'confirmed')"
        "  OR EXISTS (SELECT 1 FROM reservations r WHERE r.order_id = o.id AND r.status <> 'confirmed'))",
    ),
    Check(
        "no_double_charge",
        "the provider captured each order's payment at most once, and only paid orders are charged",
        "SELECT p.order_id FROM payments p JOIN mock_charges c ON c.provider_key = p.provider_key"
        " WHERE c.outcome = 'succeeded' AND NOT c.refunded AND p.status <> 'succeeded'",
    ),
    Check(
        "refunds_match_lost_seats",
        "a succeeded payment is never attached to an order that did not get its seats",
        "SELECT p.order_id FROM payments p JOIN orders o ON o.id = p.order_id"
        " WHERE p.status = 'succeeded' AND o.status <> 'confirmed'",
    ),
    Check(
        "one_confirmation_per_order",
        "once the outbox is drained, each confirmed order has exactly one event and one message",
        "SELECT o.id FROM orders o WHERE o.status = 'confirmed' AND ("
        " (SELECT count(*) FROM outbox x WHERE x.topic = 'order.confirmed'"
        "  AND (x.payload->>'order_id')::bigint = o.id) <> 1"
        " OR (SELECT count(*) FROM mailbox m WHERE m.order_id = o.id AND m.kind = 'confirmation') <> 1)",
    ),
    Check(
        "seats_match_reservations",
        "no seat row points at a reservation that is no longer active",
        "SELECT s.id FROM seats s JOIN reservations r ON r.id = s.reservation_id"
        " WHERE r.status NOT IN ('held', 'confirmed')",
    ),
)


def run_checks(conn: Conn) -> dict[str, list[dict[str, object]]]:
    """Return {check name: violating rows}, with an empty list for every check that passes."""
    return {c.name: list(conn.execute(c.sql).fetchall()) for c in CHECKS}


def violations(conn: Conn) -> dict[str, list[dict[str, object]]]:
    return {name: rows for name, rows in run_checks(conn).items() if rows}


def oversold_seats(conn: Conn) -> int:
    """Total reservations beyond capacity across all events (0 when nothing oversold)."""
    row = conn.execute(
        "SELECT COALESCE(sum(extra), 0)::int AS n FROM ("
        " SELECT count(r.id) - e.capacity AS extra FROM events e"
        " JOIN reservations r ON r.event_id = e.id AND r.status IN ('held', 'confirmed')"
        " GROUP BY e.id HAVING count(r.id) > e.capacity) x"
    ).fetchone()
    return int(row["n"]) if row else 0
