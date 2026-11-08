"""Events, holds and checkout through the web pages. Each test names the audit finding it closes."""

from typing import Any

from fastapi.testclient import TestClient

from festrush import worker
from tests.conftest import Db, csrf_token, login, signup


def count(db: Db, table: str, where: str = "true") -> int:
    row = db.execute(f"SELECT count(*) AS n FROM {table} WHERE {where}").fetchone()
    assert row
    return int(row["n"])


def hold(client: TestClient, event_id: int | str) -> Any:
    return client.post(
        f"/register_event/{event_id}",
        data={"csrf_token": csrf_token(client, "/login")},
        follow_redirects=False,
    )


def pay(client: TestClient, key: str = "key-1") -> Any:
    return client.post(
        "/checkout",
        data={"csrf_token": csrf_token(client, "/login"), "idempotency_key": key},
        follow_redirects=False,
    )


def sign_in(client: TestClient, name: str) -> None:
    signup(client, name)
    login(client, name)


def test_events_page_lists_events_with_fees_and_seats_left(client: TestClient, make_event: Any) -> None:
    make_event("Hackathon", 500, 3)
    make_event("Dance Battle", 300, 80)
    r = client.get("/events")
    assert r.status_code == 200
    assert "Hackathon" in r.text and "₹500" in r.text
    assert "3 of 3 seats left" in r.text
    assert "80 of 80 seats left" in r.text


def test_anonymous_visitors_are_sent_to_sign_in_not_to_a_hold(
    client: TestClient, make_event: Any, db: Db
) -> None:
    # S2: v1 let anyone register anyone with a bare GET.
    r = hold(client, make_event())
    assert (r.status_code, r.headers["location"]) == (303, "/login")
    assert count(db, "reservations") == 0


def test_holding_by_get_is_not_possible(user_client: TestClient, make_event: Any) -> None:
    assert user_client.get(f"/register_event/{make_event()}", follow_redirects=False).status_code == 405


def test_the_user_query_parameter_no_longer_means_anything(client: TestClient, make_event: Any) -> None:
    make_event()
    sign_in(client, "asha")
    page = client.get("/my-events", params={"user": "someone-else"}).text
    assert "someone-else" not in page
    assert "asha" in page


def test_holding_a_seat_creates_one_hold_and_takes_one_seat(
    user_client: TestClient, make_event: Any, db: Db
) -> None:
    event_id = make_event(capacity=3)
    r = hold(user_client, event_id)
    assert r.headers["location"] == "/my-events?notice=held"
    assert count(db, "reservations", "status = 'held'") == 1
    assert "2 of 3 seats left" in user_client.get("/events").text


def test_holding_twice_is_harmless(user_client: TestClient, make_event: Any, db: Db) -> None:
    # A4: v1 created a duplicate row on every click or retry.
    event_id = make_event(capacity=3)
    for _ in range(3):
        hold(user_client, event_id)
    assert count(db, "reservations") == 1
    assert user_client.get("/my-events").text.count("Hackathon") == 1
    assert hold(user_client, event_id).headers["location"] == "/my-events?notice=already"


def test_holding_for_a_missing_event_is_a_404(user_client: TestClient, db: Db) -> None:
    # A5: v1 accepted any event id.
    assert hold(user_client, 999).status_code == 404
    assert count(db, "reservations") == 0


def test_a_non_numeric_event_id_is_a_422_not_a_crash(user_client: TestClient) -> None:
    assert hold(user_client, "abc").status_code == 422


def test_event_404_is_just_another_missing_event(user_client: TestClient) -> None:
    # A7: v1 divided by zero on purpose for event 404.
    assert hold(user_client, 404).status_code == 404


def test_a_full_event_refuses_new_holds(client: TestClient, make_event: Any, db: Db) -> None:
    # A6: v1 had no concept of capacity.
    event_id = make_event(capacity=1)
    for name in ("asha", "ravi"):
        sign_in(client, name)
        outcome = hold(client, event_id)
        client.cookies.clear()
    assert outcome.headers["location"] == "/events?notice=full"
    assert count(db, "reservations") == 1
    assert "Sold out" in client.get("/events").text


def test_my_events_shows_only_my_seats(client: TestClient, make_event: Any) -> None:
    hack, dance = make_event("Hackathon"), make_event("Dance Battle")
    sign_in(client, "asha")
    hold(client, hack)
    client.cookies.clear()
    sign_in(client, "ravi")
    hold(client, dance)
    page = client.get("/my-events").text
    assert "Dance Battle" in page and "Hackathon" not in page


def test_my_events_and_checkout_require_sign_in(client: TestClient) -> None:
    for path in ("/my-events", "/checkout"):
        r = client.get(path, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/login")


def test_checkout_totals_only_my_holds(client: TestClient, make_event: Any) -> None:
    # A3: v1 summed the fee of every event in the database for everyone.
    hack, dance = make_event("Hackathon", 500), make_event("Dance Battle", 300)
    make_event("Robo Race", 450)
    sign_in(client, "asha")
    hold(client, hack)
    hold(client, dance)
    page = client.get("/checkout").text
    assert "Pay ₹800" in page
    assert "Robo Race" not in page


def test_checkout_knows_who_is_signed_in(user_client: TestClient) -> None:
    # A3: v1 rendered checkout as if nobody were logged in.
    page = user_client.get("/checkout").text
    assert "Signed in as" in page
    assert "Nothing to pay" in page


def test_paying_confirms_the_seat_and_the_worker_sends_the_message(
    user_client: TestClient, make_event: Any, db: Db, app_state: Any
) -> None:
    event_id = make_event("Hackathon", 500, 3)
    hold(user_client, event_id)
    r = pay(user_client)
    assert r.status_code == 303
    order_url = r.headers["location"]
    assert order_url.startswith("/orders/")
    assert count(db, "orders", "status = 'confirmed'") == 1
    assert count(db, "reservations", "status = 'confirmed'") == 1

    worker.run_once(app_state.pool, app_state.settings, app_state.provider)
    page = user_client.get(order_url).text
    assert "Paid and confirmed" in page
    assert "Your FestRush tickets are confirmed" in page
    assert "Confirmed ✓" in user_client.get("/my-events").text


def test_double_submitting_checkout_pays_once(user_client: TestClient, make_event: Any, db: Db) -> None:
    hold(user_client, make_event())
    first, second = pay(user_client, "same-key"), pay(user_client, "same-key")
    assert first.headers["location"] == second.headers["location"]
    assert count(db, "orders") == 1
    assert count(db, "payments") == 1
    assert count(db, "mock_charges") == 1


def test_checkout_with_nothing_on_hold_redirects(user_client: TestClient) -> None:
    r = pay(user_client)
    assert r.headers["location"] == "/my-events?notice=nothing"


def test_other_users_orders_are_not_visible(client: TestClient, make_event: Any) -> None:
    sign_in(client, "asha")
    hold(client, make_event())
    order_url = pay(client).headers["location"]
    client.cookies.clear()
    sign_in(client, "ravi")
    assert client.get(order_url).status_code == 404


def test_usernames_are_escaped_in_pages_and_links(client: TestClient) -> None:
    # A14 / XSS: names are limited to a safe alphabet, and everything is escaped anyway.
    assert signup(client, "<script>alert(1)</script>").status_code == 422


def test_root_redirects_to_events(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/events")


def test_health_and_readiness(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
