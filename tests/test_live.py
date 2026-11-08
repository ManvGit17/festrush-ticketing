"""The live seat map, its event stream and the System Lens."""

import json
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import Db


def test_the_seat_map_counts_free_held_sold_and_yours(
    user_client: TestClient, make_event: Any, make_user: Any, db: Db
) -> None:
    event_id = make_event("Hackathon", 500, 5)
    me = db.execute("SELECT id FROM users WHERE username = 'asha'").fetchone()
    other = make_user("ravi")
    assert me is not None
    for uid, status in ((me["id"], "held"), (other, "confirmed")):
        db.execute(
            "INSERT INTO reservations (user_id, event_id, status, expires_at)"
            " VALUES (%s, %s, %s, now() + interval '10 minutes')",
            (uid, event_id, status),
        )
    snap = user_client.get(f"/live/events/{event_id}/seats").json()
    assert sorted(snap["cells"]) == ["free", "free", "free", "mine_held", "sold"]
    assert snap["free"] == 3


def test_the_page_renders_and_unknown_events_404(client: TestClient, make_event: Any) -> None:
    event_id = make_event("Hackathon", 500, 2)
    assert "2 of 2 seats free" in client.get(f"/events/{event_id}/live").text
    assert client.get("/events/9999/live").status_code == 404


def test_the_stream_sends_the_current_map(client: TestClient, make_event: Any) -> None:
    event_id = make_event("Hackathon", 500, 2)
    r = client.get(f"/live/events/{event_id}/stream?once=true")
    assert r.headers["content-type"].startswith("text/event-stream")
    data = [line for line in r.text.splitlines() if line.startswith("data: ")]
    assert json.loads(data[0][6:])["cells"] == ["free", "free"]


def test_the_lens_needs_a_session_and_reports_system_numbers(
    client: TestClient, user_client: TestClient
) -> None:
    anonymous = TestClient(client.app)
    assert anonymous.get("/live/lens").status_code == 401
    body = user_client.get("/live/lens").json()
    assert {"queue_waiting", "outbox_pending", "db_lock_waits", "holds_active"} <= body.keys()
    assert 'id="lens"' in user_client.get("/events").text
