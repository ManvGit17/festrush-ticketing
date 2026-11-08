"""The worker: delivering the outbox, surviving failures and being killed mid-flight."""

import os
import subprocess
import sys
import time
from typing import Any

import pytest

from festrush import invariants, inventory, orders, worker
from festrush.config import REPO_ROOT
from festrush.mockpay import MockProvider
from tests.conftest import TEST_DATABASE_URL, Db, make_settings


def confirmed_orders(app_state: Any, make_event: Any, make_user: Any, n: int) -> list[int]:
    """Make n users each hold a seat, then order and pay: n confirmed orders, n outbox events."""
    event_id = make_event("Hackathon", 500, n + 5)
    provider = MockProvider(app_state.pool)
    ids = []
    for i in range(n):
        user = make_user(f"user{i:03d}")
        with app_state.pool.connection() as conn:
            inventory.reserve(conn, app_state.settings, user, event_id)
            ref = orders.create_order(conn, user)
        assert ref.id is not None
        orders.run_payment(app_state.pool, provider, ref.id)
        ids.append(ref.id)
    return ids


def unlocked_pending(db: Db) -> int:
    """Pending outbox rows nobody has locked. Fewer than all of them means a worker is mid-batch."""
    with db.transaction():
        row = db.execute(
            "SELECT count(*) AS n FROM (SELECT id FROM outbox WHERE processed_at IS NULL"
            " FOR UPDATE SKIP LOCKED) x"
        ).fetchone()
    assert row
    return int(row["n"])


def mail_count(db: Db) -> int:
    row = db.execute("SELECT count(*) AS n FROM mailbox").fetchone()
    assert row
    return int(row["n"])


def test_each_confirmed_order_gets_exactly_one_message(
    app_state: Any, make_event: Any, make_user: Any, db: Db
) -> None:
    confirmed_orders(app_state, make_event, make_user, 6)
    assert worker.process_outbox(app_state.pool, app_state.settings) == 6
    assert mail_count(db) == 6
    assert worker.process_outbox(app_state.pool, app_state.settings) == 0  # drained
    assert invariants.violations(db) == {}


def test_redelivery_does_not_duplicate_the_message(
    app_state: Any, make_event: Any, make_user: Any, db: Db
) -> None:
    """If the 'processed' mark were lost the event would be handled again; the message must not
    be sent twice. (The consumer is idempotent: one mailbox row per order and kind.)"""
    confirmed_orders(app_state, make_event, make_user, 3)
    worker.process_outbox(app_state.pool, app_state.settings)
    db.execute("UPDATE outbox SET processed_at = NULL")
    worker.process_outbox(app_state.pool, app_state.settings)
    assert mail_count(db) == 3


def test_a_failing_event_backs_off_and_does_not_block_the_others(
    app_state: Any, make_event: Any, make_user: Any, db: Db
) -> None:
    confirmed_orders(app_state, make_event, make_user, 3)
    db.execute("UPDATE outbox SET topic = 'order.unknown' WHERE id = (SELECT min(id) FROM outbox)")
    worker.process_outbox(app_state.pool, app_state.settings)
    assert mail_count(db) == 2
    bad = db.execute(
        "SELECT attempts, last_error, available_at > now() AS later FROM outbox WHERE processed_at IS NULL"
    ).fetchone()
    assert bad
    assert bad["attempts"] == 1 and bad["later"] is True
    assert bad["last_error"]


def test_an_event_that_keeps_failing_is_parked_as_dead(
    app_state: Any, make_event: Any, make_user: Any, db: Db
) -> None:
    confirmed_orders(app_state, make_event, make_user, 1)
    db.execute("UPDATE outbox SET topic = 'order.unknown'")
    settings = make_settings(outbox_max_attempts=2)
    for _ in range(2):
        db.execute("UPDATE outbox SET available_at = now()")
        worker.process_outbox(app_state.pool, settings)
    row = db.execute("SELECT dead_at FROM outbox").fetchone()
    assert row
    assert row["dead_at"] is not None
    db.execute("UPDATE outbox SET available_at = now()")
    assert worker.process_outbox(app_state.pool, settings) == 0  # dead events are left alone


def test_run_once_expires_holds_relays_events_and_settles_payments(
    app_state: Any, make_event: Any, make_user: Any, db: Db
) -> None:
    confirmed_orders(app_state, make_event, make_user, 2)
    user = make_user("late")
    event_id = make_event("Dance Battle", 300, 5)
    with app_state.pool.connection() as conn:
        inventory.reserve(conn, app_state.settings, user, event_id)
    db.execute("UPDATE reservations SET expires_at = now() - interval '1 minute' WHERE user_id = %s", (user,))
    assert worker.run_once(app_state.pool, app_state.settings, MockProvider(app_state.pool)) >= 3
    assert mail_count(db) == 2
    row = db.execute("SELECT status FROM reservations WHERE user_id = %s", (user,)).fetchone()
    assert row
    assert row["status"] == "expired"


@pytest.mark.slow
def test_killing_the_worker_mid_batch_loses_no_confirmation(
    app_state: Any, make_event: Any, make_user: Any, db: Db
) -> None:
    """The real thing: start the worker as its own process, kill it while it is part-way through
    the outbox, start another, and check every order was confirmed once and only once."""
    total = 12
    confirmed_orders(app_state, make_event, make_user, total)
    env = {
        **os.environ,
        "FESTRUSH_DATABASE_URL": TEST_DATABASE_URL,
        "FESTRUSH_SECRET_KEY": "test-secret-key-0123456789",
        "FESTRUSH_WORKER_HANDLER_DELAY_MS": "150",  # slow enough to catch it in the middle
    }
    cmd = [sys.executable, "-m", "festrush", "worker"]
    victim = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL)  # noqa: S603
    try:
        # Wait until the worker holds some outbox rows locked: it is now part-way through a batch.
        deadline = time.monotonic() + 30
        while unlocked_pending(db) == total and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        victim.kill()  # no chance to clean up
        victim.wait()
    assert mail_count(db) < total, "the kill should have landed before the batch finished"

    survivor_env = {**env, "FESTRUSH_WORKER_HANDLER_DELAY_MS": "0"}
    survivor = subprocess.Popen(cmd, cwd=REPO_ROOT, env=survivor_env, stdout=subprocess.DEVNULL)  # noqa: S603
    try:
        deadline = time.monotonic() + 30
        while mail_count(db) < total and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        survivor.kill()
        survivor.wait()
    assert mail_count(db) == total
    per_order = db.execute("SELECT count(*) AS n FROM mailbox GROUP BY order_id").fetchall()
    assert {r["n"] for r in per_order} == {1}
    assert invariants.violations(db) == {}
