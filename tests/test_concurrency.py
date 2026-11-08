"""Many requests at once. These are small versions of what the load tests do at scale."""

import threading
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from festrush import services
from festrush.services import Outcome
from tests.conftest import Db


def make_users(db: Db, n: int) -> list[int]:
    ids = []
    for i in range(n):
        row = db.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, 'x') RETURNING id",
            (f"user{i:03d}",),
        ).fetchone()
        assert row
        ids.append(int(row["id"]))
    return ids


def run_together(client: TestClient, jobs: list[tuple[int, int]]) -> list[Outcome]:
    pool = client.app.state.festrush.pool  # type: ignore[attr-defined]
    results: list[Outcome | None] = [None] * len(jobs)
    gate = threading.Barrier(len(jobs))

    def run(i: int, user_id: int, event_id: int) -> None:
        gate.wait()
        with pool.connection() as conn:
            results[i] = services.register_for_event(conn, user_id, event_id)

    threads = [threading.Thread(target=run, args=(i, u, e)) for i, (u, e) in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r is not None for r in results)
    return [r for r in results if r is not None]


def test_a_double_click_storm_registers_once(client: TestClient, db: Db, make_event: Any) -> None:
    # A4: 100 simultaneous clicks created 100 rows in v1.
    event_id = make_event(capacity=50)
    (user_id,) = make_users(db, 1)
    outcomes = run_together(client, [(user_id, event_id)] * 15)
    assert outcomes.count(Outcome.REGISTERED) == 1
    assert outcomes.count(Outcome.ALREADY) == 14
    rows = db.execute("SELECT count(*) AS n FROM registrations").fetchone()
    seats = db.execute("SELECT seats_taken FROM events").fetchone()
    assert rows and seats
    assert rows["n"] == 1
    assert seats["seats_taken"] == 1


def test_a_rush_for_a_few_seats_never_oversells(
    client: TestClient, db: Db, make_event: Any
) -> None:
    # A6: the point of the whole project, at the smallest scale.
    capacity, buyers = 5, 40
    event_id = make_event(capacity=capacity)
    users = make_users(db, buyers)
    outcomes = run_together(client, [(u, event_id) for u in users])
    assert outcomes.count(Outcome.REGISTERED) == capacity
    assert outcomes.count(Outcome.FULL) == buyers - capacity
    rows = db.execute("SELECT count(*) AS n FROM registrations").fetchone()
    seats = db.execute("SELECT seats_taken FROM events").fetchone()
    assert rows and seats
    assert rows["n"] == capacity
    assert seats["seats_taken"] == capacity


def test_counters_always_match_the_registrations(
    client: TestClient, db: Db, make_event: Any
) -> None:
    events = [make_event(f"Event {i}", 100, capacity=4) for i in range(3)]
    users = make_users(db, 24)
    run_together(client, [(u, events[i % 3]) for i, u in enumerate(users)])
    mismatches = db.execute(
        "SELECT e.id FROM events e WHERE e.seats_taken <> "
        "(SELECT count(*) FROM registrations r WHERE r.event_id = e.id)"
    ).fetchall()
    assert mismatches == []


def test_the_database_itself_refuses_to_exceed_capacity(db: Db, make_event: Any) -> None:
    # The constraint is the backstop if application code is ever wrong.
    event_id = make_event(capacity=2)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE events SET seats_taken = 3 WHERE id = %s", (event_id,))


def test_the_database_refuses_orphans_and_duplicates(db: Db, make_event: Any) -> None:
    # D1: v1 had no foreign keys and no uniqueness on registrations.
    event_id = make_event()
    (user_id,) = make_users(db, 1)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("INSERT INTO registrations (user_id, event_id) VALUES (%s, 999)", (user_id,))
    db.execute("INSERT INTO registrations (user_id, event_id) VALUES (%s, %s)", (user_id, event_id))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO registrations (user_id, event_id) VALUES (%s, %s)", (user_id, event_id)
        )
