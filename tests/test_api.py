"""The JSON API."""

from typing import Any

from fastapi.testclient import TestClient

from festrush.app import create_app
from tests.conftest import Db, make_settings


def auth(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def test_login_returns_a_token_that_works(client: TestClient, make_event: Any) -> None:
    from tests.conftest import signup

    signup(client, "asha")
    r = client.post("/api/login", json={"username": "asha", "password": "correct-horse-battery"})
    assert r.status_code == 200
    token = r.json()["token"]
    make_event()
    assert client.get("/api/me/reservations", headers=auth(token)).json() == []


def test_a_wrong_password_is_a_401_json_error(client: TestClient) -> None:
    r = client.post("/api/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401
    assert r.json() == {"error": "invalid username or password"}


def test_the_api_needs_a_token(client: TestClient, make_event: Any) -> None:
    event_id = make_event()
    r = client.post(f"/api/events/{event_id}/reserve")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    assert client.post(f"/api/events/{event_id}/reserve", headers=auth("forged")).status_code == 401


def test_events_are_public(client: TestClient, make_event: Any) -> None:
    event_id = make_event("Hackathon", 500, 3)
    listing = client.get("/api/events").json()
    assert listing[0]["name"] == "Hackathon" and listing[0]["seats_left"] == 3
    assert client.get(f"/api/events/{event_id}").json()["seats_left"] == 3
    assert client.get("/api/events/9999").status_code == 404


def test_reserve_pay_and_read_back(
    client: TestClient, make_event: Any, make_user: Any, make_token: Any
) -> None:
    event_id = make_event("Hackathon", 500, 3)
    token = make_token(make_user("asha"))
    r = client.post(f"/api/events/{event_id}/reserve", headers=auth(token))
    assert r.status_code == 201 and r.json()["status"] == "held"
    assert client.get(f"/api/events/{event_id}").json()["seats_left"] == 2

    order = client.post("/api/orders", headers=auth(token))
    assert order.status_code == 201
    assert order.json()["total"] == 500 and order.json()["status"] == "pending"

    paid = client.post(f"/api/orders/{order.json()['id']}/pay", headers=auth(token))
    assert paid.status_code == 200
    assert paid.json()["status"] == "confirmed" and paid.json()["payment_status"] == "succeeded"
    read = client.get(f"/api/orders/{order.json()['id']}", headers=auth(token)).json()
    assert read["items"][0]["name"] == "Hackathon"


def test_sold_out_and_duplicate_are_409(
    client: TestClient, make_event: Any, make_user: Any, make_token: Any
) -> None:
    event_id = make_event(capacity=1)
    a, b = make_token(make_user("asha")), make_token(make_user("ravi"))
    assert client.post(f"/api/events/{event_id}/reserve", headers=auth(a)).status_code == 201
    assert client.post(f"/api/events/{event_id}/reserve", headers=auth(a)).json() == {
        "error": "already_reserved"
    }
    r = client.post(f"/api/events/{event_id}/reserve", headers=auth(b))
    assert (r.status_code, r.json()) == (409, {"error": "sold_out"})
    assert client.post("/api/events/999/reserve", headers=auth(a)).status_code == 404


def test_a_retry_with_the_same_key_returns_the_first_answer(
    client: TestClient, make_event: Any, make_user: Any, make_token: Any, db: Db
) -> None:
    event_id = make_event(capacity=1)
    token = make_token(make_user("asha"))
    headers = auth(token, **{"Idempotency-Key": "try-1"})
    first = client.post(f"/api/events/{event_id}/reserve", headers=headers)
    second = client.post(f"/api/events/{event_id}/reserve", headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    row = db.execute("SELECT count(*) AS n FROM reservations").fetchone()
    assert row
    assert row["n"] == 1


def test_reusing_a_key_for_another_request_is_a_422(
    client: TestClient, make_event: Any, make_user: Any, make_token: Any
) -> None:
    a, b = make_event("One"), make_event("Two")
    headers = auth(make_token(make_user("asha")), **{"Idempotency-Key": "same"})
    assert client.post(f"/api/events/{a}/reserve", headers=headers).status_code == 201
    r = client.post(f"/api/events/{b}/reserve", headers=headers)
    assert (r.status_code, r.json()) == (422, {"error": "idempotency_key_reused"})


def test_an_order_with_nothing_held_is_a_409(client: TestClient, make_user: Any, make_token: Any) -> None:
    r = client.post("/api/orders", headers=auth(make_token(make_user("asha"))))
    assert (r.status_code, r.json()) == (409, {"error": "no_holds"})


def test_orders_belong_to_their_owner(
    client: TestClient, make_event: Any, make_user: Any, make_token: Any
) -> None:
    event_id = make_event()
    a, b = make_token(make_user("asha")), make_token(make_user("ravi"))
    client.post(f"/api/events/{event_id}/reserve", headers=auth(a))
    order_id = client.post("/api/orders", headers=auth(a)).json()["id"]
    assert client.get(f"/api/orders/{order_id}", headers=auth(b)).status_code == 404
    assert client.post(f"/api/orders/{order_id}/pay", headers=auth(b)).status_code == 404


def test_api_errors_are_json_not_html(client: TestClient) -> None:
    r = client.get("/api/events/abc")
    assert r.status_code == 422
    assert r.json() == {"error": "invalid_request"}
    assert client.get("/api/nope").json() == {"error": "Not Found"}


def test_the_openapi_schema_documents_the_api() -> None:
    with TestClient(create_app(make_settings(debug=True))) as c:
        spec = c.get("/openapi.json").json()
    assert "/api/events/{event_id}/reserve" in spec["paths"]
    params = spec["paths"]["/api/events/{event_id}/reserve"]["post"]["parameters"]
    assert any(p["name"] == "Idempotency-Key" for p in params)
