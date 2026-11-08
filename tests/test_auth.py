"""Accounts and sessions. Each test names the audit finding it closes."""

from fastapi.testclient import TestClient

from festrush.config import Settings
from tests.conftest import PASSWORD, Db, csrf_token, login, make_settings, post, signup


def test_signup_redirects_to_login(client: TestClient) -> None:
    r = signup(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?notice=created"


def test_password_is_hashed_with_argon2id(client: TestClient, db: Db) -> None:
    # S1: v1 stored the password as typed.
    signup(client, "asha", PASSWORD)
    stored = db.execute("SELECT password_hash FROM users WHERE username = 'asha'").fetchone()
    assert stored
    assert stored["password_hash"].startswith("$argon2id$")
    assert PASSWORD not in stored["password_hash"]


def test_usernames_are_unique_ignoring_case(client: TestClient) -> None:
    signup(client, "Asha")
    r = signup(client, "asha")
    assert r.status_code == 409
    assert "taken" in r.text


def test_empty_and_malformed_credentials_are_rejected(client: TestClient) -> None:
    # S6: v1 created accounts with a blank name and a blank password.
    assert signup(client, "", "").status_code == 422
    assert signup(client, "ab", PASSWORD).status_code == 422
    assert signup(client, "has space", PASSWORD).status_code == 422
    assert signup(client, "asha", "short").status_code == 422
    assert signup(client, "x" * 33, PASSWORD).status_code == 422


def test_failed_signup_does_not_create_a_user(client: TestClient, db: Db) -> None:
    signup(client, "", "")
    row = db.execute("SELECT count(*) AS n FROM users").fetchone()
    assert row
    assert row["n"] == 0


def test_login_sets_an_httponly_session_cookie_and_keeps_the_name_out_of_the_url(
    client: TestClient,
) -> None:
    # S2: v1 passed the username around as ?user=.
    signup(client)
    r = login(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/events"
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("festrush_session="))
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie or "samesite=lax" in cookie.lower()
    assert "asha" not in cookie


def test_wrong_password_and_unknown_user_look_the_same(client: TestClient) -> None:
    signup(client)
    wrong = login(client, "asha", "not-the-password")
    unknown = login(client, "nobody", PASSWORD)
    assert wrong.status_code == unknown.status_code == 401
    assert "Invalid username or password" in wrong.text
    assert "Invalid username or password" in unknown.text


def test_login_is_not_sql_injectable(client: TestClient) -> None:
    signup(client)
    r = login(client, "asha' --", "x")
    assert r.status_code == 401


def test_repeated_failures_are_throttled(client: TestClient, settings: Settings) -> None:
    # S7: v1 allowed unlimited guesses.
    signup(client)
    for _ in range(settings.login_max_failures):
        assert login(client, "asha", "wrong-password").status_code == 401
    blocked = login(client, "asha", PASSWORD)  # even the right password is refused for now
    assert blocked.status_code == 429


def test_throttling_is_per_username_so_others_are_unaffected(client: TestClient) -> None:
    signup(client, "asha")
    signup(client, "ravi")
    for _ in range(5):
        login(client, "asha", "wrong-password")
    assert login(client, "ravi").status_code == 303


def test_a_successful_login_clears_earlier_failures(client: TestClient) -> None:
    signup(client)
    for _ in range(3):
        login(client, "asha", "wrong-password")
    assert login(client).status_code == 303
    for _ in range(3):
        assert login(client, "asha", "wrong-password").status_code == 401


def test_logout_ends_the_session(user_client: TestClient) -> None:
    # A13: v1's "Logout" was a link to /login and ended nothing.
    assert user_client.get("/my-events", follow_redirects=False).status_code == 200
    token = csrf_token(user_client, "/events")
    old_cookie = user_client.cookies.get("festrush_session")
    assert old_cookie
    r = user_client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    # Replaying the old cookie must not work: the session is gone server-side.
    user_client.cookies.set("festrush_session", old_cookie)
    assert user_client.get("/my-events", follow_redirects=False).status_code == 303


def test_expired_sessions_are_rejected(user_client: TestClient, db: Db) -> None:
    db.execute("UPDATE sessions SET expires_at = now() - interval '1 minute'")
    r = user_client.get("/my-events", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_a_forged_session_cookie_is_rejected(client: TestClient) -> None:
    client.cookies.set("festrush_session", "asha")
    assert client.get("/my-events", follow_redirects=False).status_code == 303


def test_each_login_gets_a_new_session_token(client: TestClient) -> None:
    signup(client)
    first = login(client).headers.get_list("set-cookie")[-1].split(";")[0]
    second = login(client).headers.get_list("set-cookie")[-1].split(";")[0]
    assert first != second


def test_cookies_are_marked_secure_when_configured() -> None:
    from fastapi.testclient import TestClient as TC

    from festrush.app import create_app

    with TC(create_app(make_settings(cookie_secure=True)), base_url="https://testserver") as c:
        signup(c)
        r = login(c)
        cookie = next(x for x in r.headers.get_list("set-cookie") if x.startswith("festrush_session"))
        assert "Secure" in cookie


def test_posting_without_a_csrf_token_is_forbidden(client: TestClient) -> None:
    r = client.post("/register", data={"username": "asha", "password": PASSWORD})
    assert r.status_code == 403


def test_a_forged_csrf_token_is_forbidden(client: TestClient) -> None:
    client.get("/register")  # receive the CSRF cookie
    r = post(client, "/register", {"username": "asha", "password": PASSWORD}, token="forged.token")
    assert r.status_code == 403


def test_a_token_from_another_browser_is_forbidden(client: TestClient, settings: Settings) -> None:
    from festrush.app import create_app

    with TestClient(create_app(settings)) as other:
        foreign = csrf_token(other, "/register")
    client.get("/register")
    r = post(client, "/register", {"username": "asha", "password": PASSWORD}, token=foreign)
    assert r.status_code == 403
