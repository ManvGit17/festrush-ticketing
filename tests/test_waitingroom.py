"""The waiting room: joining, positions, admission order, the gate on reserving."""

from typing import Any

from fastapi.testclient import TestClient

from festrush import waitingroom, worker
from festrush.app import create_app
from tests.conftest import Db, make_settings


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def waiting_users(make_user: Any, db: Db, n: int) -> list[int]:
    users = [make_user(f"user{i:03d}") for i in range(n)]
    for u in users:
        db.execute("INSERT INTO queue_entries (user_id) VALUES (%s)", (u,))
    return users


def admitted(db: Db) -> set[int]:
    return {
        r["user_id"] for r in db.execute("SELECT user_id FROM queue_entries WHERE admitted_at IS NOT NULL")
    }


def test_joining_twice_keeps_the_place(make_user: Any, db: Db, app_state: Any) -> None:
    first, second = make_user("asha"), make_user("ravi")
    with app_state.pool.connection() as conn:
        waitingroom.join(conn, first)
        waitingroom.join(conn, second)
        waitingroom.join(conn, first)  # again
        assert waitingroom.status(conn, app_state.settings, first)["position"] == 1
        assert waitingroom.status(conn, app_state.settings, second)["position"] == 2


def test_status_for_someone_who_never_joined(make_user: Any, app_state: Any) -> None:
    with app_state.pool.connection() as conn:
        assert waitingroom.status(conn, app_state.settings, make_user("asha")) == {
            "joined": False,
            "admitted": False,
        }


def test_fifo_admits_the_first_arrivals(make_user: Any, db: Db, app_state: Any) -> None:
    users = waiting_users(make_user, db, 12)
    with app_state.pool.connection() as conn:
        assert waitingroom.admit_next(conn, make_settings(waiting_room_mode="fifo"), 5) == 5
    assert admitted(db) == set(users[:5])


def test_lottery_does_not_favour_the_first_arrivals(make_user: Any, db: Db, app_state: Any) -> None:
    """Admit 20 of 200 at random: the chance that it equals the first 20 is astronomically small."""
    users = waiting_users(make_user, db, 200)
    with app_state.pool.connection() as conn:
        waitingroom.admit_next(conn, make_settings(waiting_room_mode="lottery"), 20)
    got = admitted(db)
    assert len(got) == 20
    assert got != set(users[:20])
    assert len(got & set(users[:100])) < 20  # drawn from the whole queue, not from the front


def test_admission_counts_and_never_readmits(make_user: Any, db: Db, app_state: Any) -> None:
    waiting_users(make_user, db, 6)
    settings = make_settings()
    with app_state.pool.connection() as conn:
        assert waitingroom.admit_next(conn, settings, 4) == 4
        assert waitingroom.admit_next(conn, settings, 4) == 2  # only two were still waiting
        assert waitingroom.admit_next(conn, settings, 4) == 0
        assert waitingroom.admit_next(conn, settings, 0) == 0


def test_the_worker_admits_at_the_configured_rate(make_user: Any, db: Db, app_state: Any) -> None:
    waiting_users(make_user, db, 50)
    settings = make_settings(waiting_room_enabled=True, waiting_room_admit_per_second=100)
    clock = worker.AdmissionClock()
    clock.last -= 0.2  # as if 0.2 seconds had passed
    assert 15 <= worker.admit_students(app_state.pool, settings, clock) <= 25  # about 20


def test_the_worker_admits_nobody_when_the_waiting_room_is_off(
    make_user: Any, db: Db, app_state: Any
) -> None:
    waiting_users(make_user, db, 5)
    assert worker.admit_students(app_state.pool, make_settings(waiting_room_enabled=False)) == 0
    assert admitted(db) == set()


def test_an_expired_admission_no_longer_counts(make_user: Any, db: Db, app_state: Any) -> None:
    (user,) = waiting_users(make_user, db, 1)
    with app_state.pool.connection() as conn:
        waitingroom.admit_next(conn, make_settings(), 1)
        assert waitingroom.is_admitted(conn, user)
    db.execute("UPDATE queue_entries SET admitted_until = now() - interval '1 minute'")
    with app_state.pool.connection() as conn:
        assert not waitingroom.is_admitted(conn, user)
        assert waitingroom.status(conn, app_state.settings, user)["admitted"] is False


# --- The gate on reserving -----------------------------------------------------------------------


def test_reserving_needs_admission_when_the_waiting_room_is_on(
    make_event: Any, make_user: Any, make_token: Any, db: Db
) -> None:
    event_id = make_event(capacity=5)
    token = make_token(make_user("asha"))
    with TestClient(create_app(make_settings(waiting_room_enabled=True))) as client:
        refused = client.post(f"/api/events/{event_id}/reserve", headers=auth(token))
        assert (refused.status_code, refused.json()) == (403, {"error": "not_admitted"})
        joined = client.post("/api/queue/join", headers=auth(token)).json()
        assert joined["joined"] and not joined["admitted"] and joined["position"] == 1
        db.execute(
            "UPDATE queue_entries SET admitted_at = now(), admitted_until = now() + interval '5 minutes'"
        )
        assert client.get("/api/queue/status", headers=auth(token)).json()["admitted"] is True
        assert client.post(f"/api/events/{event_id}/reserve", headers=auth(token)).status_code == 201


def test_reserving_is_open_when_the_waiting_room_is_off(
    client: TestClient, make_event: Any, make_user: Any, make_token: Any
) -> None:
    token = make_token(make_user("asha"))
    assert client.post(f"/api/events/{make_event()}/reserve", headers=auth(token)).status_code == 201


def test_the_web_form_sends_unadmitted_students_to_the_queue(make_event: Any, db: Db) -> None:
    from tests.conftest import csrf_token, login, signup

    event_id = make_event()
    with TestClient(create_app(make_settings(waiting_room_enabled=True))) as client:
        signup(client, "asha")
        login(client, "asha")
        r = client.post(
            f"/register_event/{event_id}",
            data={"csrf_token": csrf_token(client, "/login")},
            follow_redirects=False,
        )
        assert (r.status_code, r.headers["location"]) == (303, "/queue")
        assert "Join the queue" in client.get("/queue").text
