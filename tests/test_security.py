"""Browser-facing hardening: headers, error pages and external requests."""

import re

from fastapi.testclient import TestClient

from festrush.app import create_app
from tests.conftest import Db, make_settings


def test_pages_carry_a_strict_content_security_policy(client: TestClient) -> None:
    r = client.get("/events")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["x-request-id"]


def test_pages_make_no_third_party_requests(client: TestClient) -> None:
    # S8: v1 loaded the Tailwind play CDN on every page.
    for path in ("/events", "/login", "/register"):
        html = client.get(path).text
        urls = re.findall(r'(?:src|href|action)="([^"]+)"', html)
        external = [u for u in urls if re.match(r"(https?:)?//", u)]
        assert external == [], f"{path} references {external}"
        assert "<script" not in html
        assert "style=" not in html  # inline styles would be blocked by the policy


def test_stylesheet_is_served_locally(client: TestClient) -> None:
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


def test_errors_do_not_show_exception_text() -> None:
    # S5: v1 rendered str(exc) into the error page.
    with TestClient(
        create_app(make_settings(debug=True)), raise_server_exceptions=False
    ) as debug_client:
        r = debug_client.get("/_debug/crash")
    assert r.status_code == 500
    assert "debug crash" not in r.text
    assert "ZeroDivisionError" not in r.text
    assert "Request ID" in r.text
    assert r.headers["content-security-policy"]


def test_the_crash_demo_does_not_exist_unless_debug_is_on(client: TestClient) -> None:
    # A7: the lab's deliberate-crash route is off by default.
    assert client.get("/_debug/crash").status_code == 404


def test_unknown_pages_get_a_friendly_404(client: TestClient) -> None:
    r = client.get("/nope")
    assert r.status_code == 404
    assert "Not found" in r.text


def test_api_docs_are_hidden_unless_debug_is_on(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_csrf_cookie_is_httponly(client: TestClient) -> None:
    r = client.get("/login")
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("festrush_csrf="))
    assert "HttpOnly" in cookie


def test_no_user_data_is_stored_in_the_session_cookie(user_client: TestClient, db: Db) -> None:
    token = user_client.cookies.get("festrush_session")
    assert token
    stored = db.execute("SELECT token_hash FROM sessions").fetchone()
    assert stored
    assert token.encode() not in bytes(stored["token_hash"])  # only a hash is kept
