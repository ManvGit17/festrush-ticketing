"""Orders, the payment state machine and the mock provider, including the awkward cases."""

import threading
from typing import Any

import pytest
from psycopg.errors import CheckViolation

from festrush import invariants, inventory, orders, worker
from festrush.mockpay import MockProvider
from tests.conftest import Db, make_settings


@pytest.fixture
def held(app_state: Any, make_event: Any, make_user: Any) -> dict[str, int]:
    """A user holding a seat at a 500-rupee event."""
    event_id, user = make_event("Hackathon", 500, 3), make_user("asha")
    with app_state.pool.connection() as conn:
        assert inventory.reserve(conn, app_state.settings, user, event_id).reservation_id
    return {"user": user, "event": event_id}


def provider(app_state: Any, **rates: Any) -> MockProvider:
    return MockProvider(app_state.pool, **rates)


def new_order(app_state: Any, user: int) -> int:
    with app_state.pool.connection() as conn:
        ref = orders.create_order(conn, user)
    assert ref.id is not None
    return ref.id


def status(db: Db, table: str, key: str, value: int) -> str:
    row = db.execute(f"SELECT status FROM {table} WHERE {key} = %s", (value,)).fetchone()
    assert row
    return str(row["status"])


def test_an_order_covers_the_users_live_holds(app_state: Any, held: dict[str, int], db: Db) -> None:
    order_id = new_order(app_state, held["user"])
    row = db.execute("SELECT total, status FROM orders WHERE id = %s", (order_id,)).fetchone()
    assert row
    assert (row["total"], row["status"]) == (500, "pending")
    with app_state.pool.connection() as conn:
        assert orders.create_order(conn, held["user"]).id is None  # nothing left to order


def test_a_successful_payment_confirms_the_order_and_queues_one_event(
    app_state: Any, held: dict[str, int], db: Db
) -> None:
    order_id = new_order(app_state, held["user"])
    assert orders.run_payment(app_state.pool, provider(app_state), order_id) == "succeeded"
    assert status(db, "orders", "id", order_id) == "confirmed"
    assert status(db, "reservations", "order_id", order_id) == "confirmed"
    events = db.execute("SELECT topic FROM outbox").fetchall()
    assert [e["topic"] for e in events] == ["order.confirmed"]


def test_paying_again_does_nothing(app_state: Any, held: dict[str, int], db: Db) -> None:
    order_id = new_order(app_state, held["user"])
    p = provider(app_state)
    for _ in range(3):
        orders.run_payment(app_state.pool, p, order_id)
    assert db.execute("SELECT count(*) AS n FROM mock_charges").fetchone()["n"] == 1  # type: ignore[index]
    assert db.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 1  # type: ignore[index]


def test_ten_simultaneous_payments_reach_the_provider_once(
    app_state: Any, held: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id = new_order(app_state, held["user"])
    p = provider(app_state, latency_ms=50)
    calls: list[str] = []
    real = MockProvider.charge

    def counting(self: MockProvider, key: str, amount: int) -> Any:
        calls.append(key)
        return real(self, key, amount)

    monkeypatch.setattr(MockProvider, "charge", counting)
    gate = threading.Barrier(10)

    def go() -> None:
        gate.wait()
        orders.run_payment(app_state.pool, p, order_id)

    threads = [threading.Thread(target=go) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1


def test_a_declined_payment_keeps_the_hold_and_allows_a_second_try(
    app_state: Any, held: dict[str, int], db: Db
) -> None:
    first = new_order(app_state, held["user"])
    assert orders.run_payment(app_state.pool, provider(app_state, decline_rate=1.0), first) == "failed"
    assert status(db, "orders", "id", first) == "payment_failed"
    assert db.execute("SELECT status FROM reservations").fetchone()["status"] == "held"  # type: ignore[index]

    second = new_order(app_state, held["user"])
    assert second != first
    assert orders.run_payment(app_state.pool, provider(app_state), second) == "succeeded"
    assert status(db, "orders", "id", second) == "confirmed"
    worker.process_outbox(app_state.pool, app_state.settings)
    assert invariants.violations(db) == {}


def test_a_lost_response_is_settled_by_the_worker(app_state: Any, held: dict[str, int], db: Db) -> None:
    """The provider took the money but the answer never arrived: the order stays pending until
    the reconciler asks, and then it confirms (a lost answer does not lose the sale)."""
    order_id = new_order(app_state, held["user"])
    flaky = provider(app_state, timeout_rate=1.0)
    assert orders.run_payment(app_state.pool, flaky, order_id) == "unknown"
    assert status(db, "orders", "id", order_id) == "pending"

    quick = make_settings(payment_reconcile_seconds=0)
    assert orders.reconcile_payments(app_state.pool, flaky, quick) == 1
    assert status(db, "orders", "id", order_id) == "confirmed"
    assert status(db, "payments", "order_id", order_id) == "succeeded"
    worker.process_outbox(app_state.pool, app_state.settings)
    assert invariants.violations(db) == {}


def test_a_charge_the_provider_never_saw_is_sent_again_not_lost(
    app_state: Any, held: dict[str, int], db: Db
) -> None:
    """The web process died after claiming the payment but before calling the provider. The
    provider has no record, so nothing was taken; the reconciler sends the charge again with the
    same key (so it is taken at most once) and the sale completes instead of being abandoned."""
    order_id = new_order(app_state, held["user"])
    db.execute("UPDATE payments SET status = 'processing' WHERE order_id = %s", (order_id,))
    quick = make_settings(payment_reconcile_seconds=0)
    assert orders.reconcile_payments(app_state.pool, provider(app_state), quick) == 1
    assert status(db, "payments", "order_id", order_id) == "succeeded"
    assert status(db, "orders", "id", order_id) == "confirmed"
    assert db.execute("SELECT count(*) AS n FROM mock_charges").fetchone()["n"] == 1  # type: ignore[index]


def test_an_unseen_charge_for_an_order_that_expired_is_not_sent(
    app_state: Any, held: dict[str, int], db: Db
) -> None:
    """Never take money for seats that are already gone."""
    order_id = new_order(app_state, held["user"])
    db.execute("UPDATE payments SET status = 'processing' WHERE order_id = %s", (order_id,))
    db.execute("UPDATE reservations SET expires_at = now() - interval '10 minutes'")
    with app_state.pool.connection() as conn:
        inventory.expire_holds(conn, make_settings(payment_grace_seconds=0))
    assert status(db, "orders", "id", order_id) == "expired"
    quick = make_settings(payment_reconcile_seconds=0)
    assert orders.reconcile_payments(app_state.pool, provider(app_state), quick) == 1
    assert status(db, "payments", "order_id", order_id) == "failed"
    assert db.execute("SELECT count(*) AS n FROM mock_charges").fetchone()["n"] == 0  # type: ignore[index]


def test_late_money_after_expiry_is_refunded(app_state: Any, held: dict[str, int], db: Db) -> None:
    """The hard case: the hold expires while the payment is in flight, then the provider says it
    took the money. The seat is gone, so the money goes back and nobody is left paid-but-seatless."""
    order_id = new_order(app_state, held["user"])
    flaky = provider(app_state, timeout_rate=1.0)
    assert orders.run_payment(app_state.pool, flaky, order_id) == "unknown"

    db.execute("UPDATE reservations SET expires_at = now() - interval '10 minutes'")
    expiring = make_settings(payment_grace_seconds=0)
    with app_state.pool.connection() as conn:
        assert inventory.expire_holds(conn, expiring) == 1
    assert status(db, "orders", "id", order_id) == "expired"

    quick = make_settings(payment_reconcile_seconds=0)
    assert orders.reconcile_payments(app_state.pool, flaky, quick) == 1
    assert status(db, "payments", "order_id", order_id) == "refunded"
    assert status(db, "orders", "id", order_id) == "refunded"
    charge = db.execute("SELECT refunded FROM mock_charges").fetchone()
    assert charge and charge["refunded"] is True
    assert status(db, "reservations", "order_id", order_id) == "expired"

    worker.process_outbox(app_state.pool, app_state.settings)
    kinds = [m["kind"] for m in db.execute("SELECT kind FROM mailbox").fetchall()]
    assert kinds == ["refund"]
    assert invariants.violations(db) == {}


def test_a_hold_with_a_payment_in_flight_survives_its_expiry_during_the_grace_period(
    app_state: Any, held: dict[str, int], db: Db
) -> None:
    order_id = new_order(app_state, held["user"])
    orders.run_payment(app_state.pool, provider(app_state, timeout_rate=1.0), order_id)
    db.execute("UPDATE reservations SET expires_at = now() - interval '10 seconds'")
    with app_state.pool.connection() as conn:
        assert inventory.expire_holds(conn, make_settings(payment_grace_seconds=120)) == 0
    assert status(db, "reservations", "order_id", order_id) == "held"


def test_applying_the_same_outcome_twice_changes_nothing(
    app_state: Any, held: dict[str, int], db: Db
) -> None:
    order_id = new_order(app_state, held["user"])
    p = provider(app_state)
    orders.run_payment(app_state.pool, p, order_id)
    payment = db.execute("SELECT id FROM payments").fetchone()
    assert payment
    with app_state.pool.connection() as conn:
        assert orders.apply_payment_outcome(conn, p, payment["id"], "succeeded") == "succeeded"
    assert db.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 1  # type: ignore[index]


def test_illegal_order_moves_are_refused_by_the_database(
    app_state: Any, held: dict[str, int], db: Db
) -> None:
    order_id = new_order(app_state, held["user"])
    with pytest.raises(CheckViolation):
        db.execute("UPDATE orders SET status = 'confirmed' WHERE id = %s", (order_id,))  # skips 'paid'
    orders.run_payment(app_state.pool, provider(app_state), order_id)
    with pytest.raises(CheckViolation):
        db.execute("UPDATE orders SET status = 'pending' WHERE id = %s", (order_id,))  # backwards
