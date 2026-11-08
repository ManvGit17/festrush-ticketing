"""The invariant checks must notice the damage they exist to catch."""

from typing import Any

from festrush import invariants
from tests.conftest import Db


def reserve_row(db: Db, user: int, event: int, status: str = "held", order: int | None = None) -> int:
    row = db.execute(
        "INSERT INTO reservations (user_id, event_id, status, order_id, expires_at)"
        " VALUES (%s, %s, %s, %s, now() + interval '1 hour') RETURNING id",
        (user, event, status, order),
    ).fetchone()
    assert row
    return int(row["id"])


def test_a_clean_database_passes_every_check(db: Db) -> None:
    assert invariants.violations(db) == {}


def test_oversell_is_detected(make_event: Any, make_user: Any, db: Db) -> None:
    event = make_event(capacity=1)
    reserve_row(db, make_user("user_a"), event)
    reserve_row(db, make_user("user_b"), event)
    assert list(invariants.violations(db)) == ["no_oversell"]
    assert invariants.oversold_seats(db) == 1


def test_a_confirmed_seat_without_a_paid_order_is_detected(make_event: Any, make_user: Any, db: Db) -> None:
    reserve_row(db, make_user("user_a"), make_event(), status="held")
    db.execute("UPDATE reservations SET status = 'confirmed'")  # skipped payment entirely
    assert "confirmed_seats_are_paid" in invariants.violations(db)


def test_a_confirmed_order_without_its_message_is_detected(make_event: Any, make_user: Any, db: Db) -> None:
    user, event = make_user("user_a"), make_event()
    order = db.execute("INSERT INTO orders (user_id, total) VALUES (%s, 1) RETURNING id", (user,)).fetchone()
    assert order
    reserve_row(db, user, event, order=order["id"])
    db.execute("INSERT INTO payments (order_id, provider_key, amount) VALUES (%s, 'k', 1)", (order["id"],))
    db.execute("UPDATE payments SET status = 'succeeded'")
    db.execute("UPDATE orders SET status = 'paid'")
    db.execute("UPDATE reservations SET status = 'confirmed'")
    db.execute("UPDATE orders SET status = 'confirmed'")
    assert "one_confirmation_per_order" in invariants.violations(db)


def test_money_taken_for_an_unconfirmed_order_is_detected(make_user: Any, db: Db) -> None:
    user = make_user("user_a")
    order = db.execute("INSERT INTO orders (user_id, total) VALUES (%s, 1) RETURNING id", (user,)).fetchone()
    assert order
    db.execute("INSERT INTO payments (order_id, provider_key, amount) VALUES (%s, 'k', 1)", (order["id"],))
    db.execute("INSERT INTO mock_charges (provider_key, amount, outcome) VALUES ('k', 1, 'succeeded')")
    assert "no_double_charge" in invariants.violations(db)
