"""Idempotency keys: a retry gets the first answer and does nothing twice."""

import threading
from typing import Any

import pytest

from festrush.db import Conn
from festrush.idempotency import Replayable, request_hash, run_idempotent
from tests.conftest import Db


@pytest.fixture
def counter(db: Db) -> Any:
    db.execute("CREATE TABLE IF NOT EXISTS effects (n integer)")
    db.execute("TRUNCATE effects")
    return db


def effects(db: Db) -> int:
    row = db.execute("SELECT count(*) AS n FROM effects").fetchone()
    assert row
    return int(row["n"])


def work(conn: Conn) -> tuple[int, dict[str, Any]]:
    conn.execute("INSERT INTO effects VALUES (1)")
    return 201, {"made": True}


def test_the_same_key_runs_the_work_once(counter: Db, make_user: Any, app_state: Any) -> None:
    user = make_user()
    first = run_idempotent(app_state.pool, user, "k1", request_hash("x"), work)
    second = run_idempotent(app_state.pool, user, "k1", request_hash("x"), work)
    assert (first.status, first.replayed) == (201, False)
    assert (second.status, second.replayed, second.body) == (201, True, {"made": True})
    assert effects(counter) == 1


def test_without_a_key_every_call_runs(counter: Db, make_user: Any, app_state: Any) -> None:
    user = make_user()
    for _ in range(3):
        run_idempotent(app_state.pool, user, None, request_hash("x"), work)
    assert effects(counter) == 3


def test_the_same_key_for_a_different_request_is_refused(counter: Db, make_user: Any, app_state: Any) -> None:
    user = make_user()
    run_idempotent(app_state.pool, user, "k1", request_hash("reserve", 1), work)
    other = run_idempotent(app_state.pool, user, "k1", request_hash("reserve", 2), work)
    assert other.mismatch and other.status == 422
    assert effects(counter) == 1


def test_keys_are_per_user(counter: Db, make_user: Any, app_state: Any) -> None:
    a, b = make_user("asha"), make_user("ravi")
    run_idempotent(app_state.pool, a, "shared", request_hash("x"), work)
    again = run_idempotent(app_state.pool, b, "shared", request_hash("x"), work)
    assert not again.replayed
    assert effects(counter) == 2


def test_simultaneous_duplicates_run_the_work_once(counter: Db, make_user: Any, app_state: Any) -> None:
    user = make_user()
    results: list[Replayable | None] = [None] * 12
    gate = threading.Barrier(12)

    def go(i: int) -> None:
        gate.wait()
        results[i] = run_idempotent(app_state.pool, user, "storm", request_hash("x"), work)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert effects(counter) == 1
    done = [r for r in results if r is not None]
    assert len(done) == 12
    assert sum(1 for r in done if not r.replayed) == 1
    assert {r.status for r in done} == {201}


def test_a_crash_inside_the_work_leaves_no_key_and_no_effect(
    counter: Db, make_user: Any, app_state: Any
) -> None:
    user = make_user()

    def crashing(conn: Conn) -> tuple[int, dict[str, Any]]:
        conn.execute("INSERT INTO effects VALUES (1)")
        raise RuntimeError("process died")

    with pytest.raises(RuntimeError):
        run_idempotent(app_state.pool, user, "k1", request_hash("x"), crashing)
    assert effects(counter) == 0
    row = counter.execute("SELECT count(*) AS n FROM idempotency_keys").fetchone()
    assert row
    assert row["n"] == 0
    retried = run_idempotent(app_state.pool, user, "k1", request_hash("x"), work)
    assert not retried.replayed
    assert effects(counter) == 1
