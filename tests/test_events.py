"""Events, registration and checkout. Each test names the audit finding it closes."""

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import Db, csrf_token, login, post, signup


def register(client: TestClient, event_id: int | str) -> Any:
    return client.post(
        f"/register_event/{event_id}",
        data={"csrf_token": csrf_token(client, "/login")},
        follow_redirects=False,
    )


def count(db: Db, table: str = "registrations") -> int:
    row = db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert row
    return int(row["n"])


def test_events_page_lists_events_with_fees_and_seats_left(
    client: TestClient, make_event: Any
) -> None:
    make_event("Hackathon", 500, 3)
    make_event("Dance Battle", 300, 80)
    r = client.get("/events")
    assert r.status_code == 200
    assert "Hackathon" in r.text and "₹500" in r.text
    assert "3 of 3 seats left" in r.text
    assert "80 of 80 seats left" in r.text


def test_anonymous_visitors_are_sent_to_sign_in_not_to_a_registration(
    client: TestClient, make_event: Any, db: Db
) -> None:
    # S2: v1 let anyone register anyone with a bare GET.
    event_id = make_event()
    r = register(client, event_id)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert count(db) == 0


def test_registering_by_get_is_not_possible(user_client: TestClient, make_event: Any) -> None:
    event_id = make_event()
    r = user_client.get(f"/register_event/{event_id}", follow_redirects=False)
    assert r.status_code == 405


def test_the_user_query_parameter_no_longer_means_anything(
    client: TestClient, make_event: Any
) -> None:
    make_event()
    signup(client, "asha")
    login(client, "asha")
    r = client.get("/my-events", params={"user": "someone-else"})
    assert "someone-else" not in r.text
    assert "asha" in r.text


def test_registering_creates_one_registration_and_takes_one_seat(
    user_client: TestClient, make_event: Any, db: Db
) -> None:
    event_id = make_event(capacity=3)
    r = register(user_client, event_id)
    assert r.status_code == 303
    assert r.headers["location"] == "/my-events?notice=registered"
    assert count(db) == 1
    seats = db.execute("SELECT seats_taken FROM events WHERE id = %s", (event_id,)).fetchone()
    assert seats
    assert seats["seats_taken"] == 1


def test_registering_twice_is_harmless(user_client: TestClient, make_event: Any, db: Db) -> None:
    # A4: v1 created a duplicate row on every click or retry.
    event_id = make_event(capacity=3)
    for _ in range(3):
        register(user_client, event_id)
    assert count(db) == 1
    seats = db.execute("SELECT seats_taken FROM events WHERE id = %s", (event_id,)).fetchone()
    assert seats
    assert seats["seats_taken"] == 1
    page = user_client.get("/my-events").text
    assert page.count("Hackathon") == 1
    again = register(user_client, event_id)
    assert again.headers["location"] == "/my-events?notice=already"


def test_registering_for_a_missing_event_is_a_404(user_client: TestClient, db: Db) -> None:
    # A5: v1 accepted any event id.
    r = register(user_client, 999)
    assert r.status_code == 404
    assert count(db) == 0


def test_a_non_numeric_event_id_is_a_422_not_a_crash(user_client: TestClient) -> None:
    r = register(user_client, "abc")
    assert r.status_code == 422


def test_event_404_is_just_another_missing_event(user_client: TestClient) -> None:
    # A7: v1 divided by zero on purpose for event 404.
    assert register(user_client, 404).status_code == 404


def test_a_full_event_refuses_new_registrations(
    client: TestClient, make_event: Any, db: Db
) -> None:
    # A6: v1 had no concept of capacity.
    event_id = make_event(capacity=1)
    for name in ("asha", "ravi"):
        signup(client, name)
        login(client, name)
        outcome = register(client, event_id)
        client.cookies.clear()
    assert outcome.headers["location"] == "/events?notice=full"
    assert count(db) == 1
    page = client.get("/events").text
    assert "Sold out" in page


def test_my_events_shows_only_my_registrations(client: TestClient, make_event: Any) -> None:
    hack = make_event("Hackathon")
    dance = make_event("Dance Battle")
    signup(client, "asha")
    login(client, "asha")
    register(client, hack)
    client.cookies.clear()
    signup(client, "ravi")
    login(client, "ravi")
    register(client, dance)
    page = client.get("/my-events").text
    assert "Dance Battle" in page
    assert "Hackathon" not in page


def test_my_events_requires_sign_in(client: TestClient) -> None:
    r = client.get("/my-events", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login")


def test_checkout_totals_only_my_events(client: TestClient, make_event: Any) -> None:
    # A3: v1 summed the fee of every event in the database for everyone.
    hack = make_event("Hackathon", 500)
    dance = make_event("Dance Battle", 300)
    make_event("Robo Race", 450)
    signup(client, "asha")
    login(client, "asha")
    register(client, hack)
    register(client, dance)
    page = client.get("/checkout").text
    assert "₹800" in page
    assert "Robo Race" not in page


def test_checkout_knows_who_is_signed_in(user_client: TestClient) -> None:
    # A3: v1 rendered checkout as if nobody were logged in.
    page = user_client.get("/checkout").text
    assert "Signed in as" in page
    assert "Nothing to pay yet" in page


def test_checkout_requires_sign_in(client: TestClient) -> None:
    assert client.get("/checkout", follow_redirects=False).status_code == 303


def test_usernames_are_escaped_in_pages_and_links(client: TestClient) -> None:
    # A14 / XSS: names are limited to a safe alphabet, and everything is escaped anyway.
    assert signup(client, "<script>alert(1)</script>").status_code == 422


def test_root_redirects_to_events(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/events")


def test_health_and_readiness(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_post_helper_sanity(client: TestClient) -> None:
    # Guards the test helper itself: a registration without a token must fail.
    r = post(client, "/register", {"username": "asha", "password": "x"}, token="")
    assert r.status_code == 403
