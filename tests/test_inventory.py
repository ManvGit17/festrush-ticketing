"""The four strategies: what they guarantee, what they do under a rush, and how holds expire."""

import threading
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation

from festrush import invariants, inventory
from festrush.config import Settings
from festrush.inventory import Outcome
from tests.conftest import Db, make_settings

SAFE = ["row_lock", "atomic_update", "skip_locked"]
ALL = ["naive", *SAFE]

Connect = Callable[[], Any]


def reserve_together(settings: Settings, jobs: list[tuple[int, int]], pool_connect: Connect) -> list[Outcome]:
    """Fire one reservation per (user, event) job at the same instant, each on its own connection."""
    results: list[Outcome | None] = [None] * len(jobs)
    gate = threading.Barrier(len(jobs))

    def run(i: int, user_id: int, event_id: int) -> None:
        with pool_connect() as conn:
            gate.wait()
            results[i] = inventory.reserve(conn, settings, user_id, event_id).outcome

    threads = [threading.Thread(target=run, args=(i, u, e)) for i, (u, e) in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r is not None for r in results)
    return [r for r in results if r is not None]


@pytest.fixture
def connect() -> Connect:
    from tests.conftest import TEST_DATABASE_URL

    def _connect() -> Any:
        return psycopg.connect(TEST_DATABASE_URL, row_factory=psycopg.rows.dict_row)

    return _connect


def one_by_one(settings: Settings, user_id: int, event_id: int, connect: Connect) -> Outcome:
    with connect() as conn:
        return inventory.reserve(conn, settings, user_id, event_id).outcome


@pytest.mark.parametrize("strategy", ALL)
def test_basic_outcomes(strategy: str, make_event: Any, make_user: Any, connect: Connect) -> None:
    settings = make_settings(inventory_strategy=strategy)
    event_id = make_event(capacity=2)
    a, b, c = make_user("asha"), make_user("ravi"), make_user("meera")
    assert one_by_one(settings, a, event_id, connect) is Outcome.HELD
    assert one_by_one(settings, a, event_id, connect) is Outcome.ALREADY
    assert one_by_one(settings, b, event_id, connect) is Outcome.HELD
    assert one_by_one(settings, c, event_id, connect) is Outcome.FULL
    assert one_by_one(settings, c, 9999, connect) is Outcome.NO_EVENT


@pytest.mark.parametrize("strategy", ALL)
def test_a_failed_claim_leaves_nothing_behind(
    strategy: str, make_event: Any, make_user: Any, connect: Connect, db: Db
) -> None:
    settings = make_settings(inventory_strategy=strategy)
    event_id = make_event(capacity=1)
    a, b = make_user("asha"), make_user("ravi")
    one_by_one(settings, a, event_id, connect)
    assert one_by_one(settings, b, event_id, connect) is Outcome.FULL
    row = db.execute("SELECT count(*) AS n FROM reservations").fetchone()
    assert row
    assert row["n"] == 1


@pytest.mark.parametrize("strategy", SAFE)
def test_a_rush_never_oversells(
    strategy: str, make_event: Any, make_user: Any, connect: Connect, db: Db
) -> None:
    settings = make_settings(inventory_strategy=strategy)
    capacity, buyers = 5, 40
    event_id = make_event(capacity=capacity)
    users = [make_user(f"user{i:03d}") for i in range(buyers)]
    outcomes = reserve_together(settings, [(u, event_id) for u in users], connect)

    held = outcomes.count(Outcome.HELD)
    assert held <= capacity
    assert invariants.oversold_seats(db) == 0
    assert invariants.violations(db) == {}
    if strategy != "skip_locked":
        assert held == capacity
        assert outcomes.count(Outcome.FULL) == buyers - capacity
    else:
        # skip_locked may answer BUSY while free seats are locked by claims still in flight.
        # Those users retry; afterwards every seat is taken and still none is oversold.
        for user in users:
            one_by_one(settings, user, event_id, connect)
        assert invariants.oversold_seats(db) == 0
        row = db.execute("SELECT count(*) AS n FROM reservations").fetchone()
        assert row
        assert row["n"] == capacity


@pytest.mark.parametrize("strategy", SAFE)
def test_a_double_click_storm_holds_one_seat(
    strategy: str, make_event: Any, make_user: Any, connect: Connect, db: Db
) -> None:
    # A4: 100 simultaneous clicks created 100 rows in v1.
    settings = make_settings(inventory_strategy=strategy)
    event_id = make_event(capacity=50)
    user = make_user()
    outcomes = reserve_together(settings, [(user, event_id)] * 15, connect)
    assert outcomes.count(Outcome.HELD) == 1
    assert outcomes.count(Outcome.ALREADY) == 14
    row = db.execute("SELECT count(*) AS n FROM reservations").fetchone()
    assert row
    assert row["n"] == 1


def test_the_naive_strategy_oversells_when_requests_overlap(
    make_event: Any, make_user: Any, connect: Connect, db: Db
) -> None:
    """The baseline, demonstrated. The delay widens the window between the count and the insert
    the way a network hop or extra logic would; without it the race is real but rarer."""
    settings = make_settings(inventory_strategy="naive", naive_delay_ms=40)
    event_id = make_event(capacity=3)
    users = [make_user(f"user{i:03d}") for i in range(20)]
    outcomes = reserve_together(settings, [(u, event_id) for u in users], connect)
    assert outcomes.count(Outcome.HELD) > 3
    assert invariants.oversold_seats(db) > 0
    assert "no_oversell" in invariants.violations(db)


@pytest.mark.parametrize("strategy", SAFE)
def test_expired_holds_give_their_seat_back(
    strategy: str, make_event: Any, make_user: Any, connect: Connect, db: Db
) -> None:
    settings = make_settings(inventory_strategy=strategy)
    event_id = make_event(capacity=2)
    a, b, c = make_user("asha"), make_user("ravi"), make_user("meera")
    one_by_one(settings, a, event_id, connect)
    one_by_one(settings, b, event_id, connect)
    assert one_by_one(settings, c, event_id, connect) is Outcome.FULL

    db.execute("UPDATE reservations SET expires_at = now() - interval '1 minute' WHERE user_id = %s", (a,))
    with connect() as conn:
        assert inventory.expire_holds(conn, settings) == 1
    row = db.execute("SELECT status FROM reservations WHERE user_id = %s", (a,)).fetchone()
    assert row
    assert row["status"] == "expired"

    assert one_by_one(settings, c, event_id, connect) is Outcome.HELD
    assert one_by_one(settings, a, event_id, connect) is Outcome.FULL
    assert invariants.violations(db) == {}


def test_an_expired_user_may_hold_again(make_event: Any, make_user: Any, connect: Connect, db: Db) -> None:
    settings = make_settings(inventory_strategy="atomic_update")
    event_id = make_event(capacity=2)
    a = make_user()
    one_by_one(settings, a, event_id, connect)
    db.execute("UPDATE reservations SET expires_at = now() - interval '1 minute'")
    with connect() as conn:
        inventory.expire_holds(conn, settings)
    assert one_by_one(settings, a, event_id, connect) is Outcome.HELD


def test_rebuild_inventory_recomputes_counters_and_seats(
    make_event: Any, make_user: Any, connect: Connect, db: Db
) -> None:
    event_id = make_event(capacity=4)
    settings = make_settings(inventory_strategy="naive")
    for i in range(3):
        one_by_one(settings, make_user(f"user{i}"), event_id, connect)
    db.execute("SELECT rebuild_inventory()")
    claimed = db.execute("SELECT claimed FROM events").fetchone()
    taken = db.execute("SELECT count(*) AS n FROM seats WHERE reservation_id IS NOT NULL").fetchone()
    seats = db.execute("SELECT count(*) AS n FROM seats").fetchone()
    assert claimed and taken and seats
    assert (claimed["claimed"], taken["n"], seats["n"]) == (3, 3, 4)
    # And the safe strategies now continue from that state.
    assert (
        one_by_one(make_settings(inventory_strategy="skip_locked"), make_user("late"), event_id, connect)
        is Outcome.HELD
    )
    assert (
        one_by_one(make_settings(inventory_strategy="skip_locked"), make_user("later"), event_id, connect)
        is Outcome.FULL
    )


def test_new_events_get_their_seats(make_event: Any, db: Db) -> None:
    make_event(capacity=7)
    row = db.execute("SELECT count(*) AS n FROM seats").fetchone()
    assert row
    assert row["n"] == 7


# --- The database's own guarantees --------------------------------------------------------------


def test_the_counter_cannot_pass_capacity(make_event: Any, db: Db) -> None:
    event_id = make_event(capacity=2)
    with pytest.raises(CheckViolation):
        db.execute("UPDATE events SET claimed = 3 WHERE id = %s", (event_id,))


def test_a_user_cannot_hold_an_event_twice_and_orphans_are_refused(
    make_event: Any, make_user: Any, db: Db
) -> None:
    # D1: v1 had no foreign keys and no uniqueness on registrations.
    event_id, user = make_event(), make_user()
    with pytest.raises(ForeignKeyViolation):
        db.execute(
            "INSERT INTO reservations (user_id, event_id, expires_at) VALUES (%s, 999, now())", (user,)
        )
    db.execute(
        "INSERT INTO reservations (user_id, event_id, expires_at) VALUES (%s, %s, now())", (user, event_id)
    )
    with pytest.raises(UniqueViolation):
        db.execute(
            "INSERT INTO reservations (user_id, event_id, expires_at) VALUES (%s, %s, now())",
            (user, event_id),
        )


def test_reservations_cannot_move_backwards(make_event: Any, make_user: Any, db: Db) -> None:
    event_id, user = make_event(), make_user()
    row = db.execute(
        "INSERT INTO reservations (user_id, event_id, expires_at, status) VALUES (%s, %s, now(), 'expired')"
        " RETURNING id",
        (user, event_id),
    ).fetchone()
    assert row
    with pytest.raises(CheckViolation):
        db.execute("UPDATE reservations SET status = 'held' WHERE id = %s", (row["id"],))
