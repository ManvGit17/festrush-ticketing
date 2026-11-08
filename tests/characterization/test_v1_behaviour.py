"""Characterization tests for the original monolith. See conftest.py for the ground rules.

Audit IDs (A1, S2, ...) refer to docs/audit.md.
"""

import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from .conftest import ROOT

Connect = Callable[[], sqlite3.Connection]


def signup(client: TestClient, username: str = "asha", password: str = "pw-asha") -> None:
    r = client.post("/register", data={"username": username, "password": password}, follow_redirects=False)
    assert r.status_code == 302


# --- Startup -------------------------------------------------------------------------------


def test_startup_creates_the_three_tables(client: TestClient, connect: Connect) -> None:
    names = {r[0] for r in connect().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "events", "registrations"} <= names


def test_registrations_have_no_keys_or_constraints(client: TestClient, connect: Connect) -> None:
    # BUG (D1, A6): no primary key, no foreign keys, no uniqueness, no capacity column.
    sql = connect().execute("SELECT sql FROM sqlite_master WHERE name='registrations'").fetchone()[0]
    assert "PRIMARY KEY" not in sql.upper()
    assert "REFERENCES" not in sql.upper()
    events_sql = connect().execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0]
    assert "capacity" not in events_sql.lower() and "seats" not in events_sql.lower()


# --- Accounts ------------------------------------------------------------------------------


def test_signup_redirects_to_login(client: TestClient) -> None:
    r = client.post("/register", data={"username": "a", "password": "b"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_duplicate_username_returns_plain_text_with_200(client: TestClient) -> None:
    signup(client)
    r = client.post("/register", data={"username": "asha", "password": "other"})
    assert r.status_code == 200
    assert r.text == "Username already exists. Try a different one."


def test_password_is_stored_in_plaintext(client: TestClient, connect: Connect) -> None:
    # BUG (S1): plaintext passwords.
    signup(client, "asha", "pw-asha")
    row = connect().execute("SELECT password FROM users WHERE username='asha'").fetchone()
    assert row[0] == "pw-asha"


def test_login_success_puts_the_username_in_the_url(client: TestClient) -> None:
    # BUG (S2): identity is a query parameter; there is no session.
    signup(client)
    r = client.post("/login", data={"username": "asha", "password": "pw-asha"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/events?user=asha"
    assert "set-cookie" not in r.headers


def test_login_failure_rerenders_with_error(client: TestClient) -> None:
    signup(client)
    r = client.post("/login", data={"username": "asha", "password": "wrong"})
    assert r.status_code == 200
    assert "Invalid username or password" in r.text


def test_login_is_not_sql_injectable(client: TestClient) -> None:
    signup(client)
    r = client.post("/login", data={"username": "asha' --", "password": "x"}, follow_redirects=False)
    assert r.status_code == 200
    assert "Invalid username or password" in r.text


def test_empty_username_and_password_create_an_account(client: TestClient, connect: Connect) -> None:
    # BUG (S6): no validation. On the dependency versions the original was written against, empty
    # strings satisfy ``Form(...)``, so an account with a blank name and password is created.
    # (Newer FastAPI releases answer 422 here, which would hide the problem.)
    r = client.post("/register", data={"username": "", "password": ""}, follow_redirects=False)
    assert r.status_code == 302
    assert connect().execute("SELECT username, password FROM users").fetchall()[0][:] == ("", "")


def test_missing_form_fields_are_rejected_with_422(client: TestClient) -> None:
    assert client.post("/register", data={}).status_code == 422


# --- Events --------------------------------------------------------------------------------


def test_events_lists_every_event_with_fee(client: TestClient) -> None:
    r = client.get("/events", params={"user": "asha"})
    assert r.status_code == 200
    for name, fee in [("Hackathon", 500), ("Dance Battle", 300), ("AI Workshop", 400)]:
        assert name in r.text
        assert f"₹ {fee}" in r.text


def test_events_requires_the_user_query_param(client: TestClient) -> None:
    assert client.get("/events").status_code == 422


def test_events_page_for_a_user_that_does_not_exist(client: TestClient) -> None:
    # BUG (S2): any string is accepted as the current user.
    r = client.get("/events", params={"user": "nobody"})
    assert r.status_code == 200
    assert "Welcome <b>nobody</b>" in r.text


def test_user_param_is_html_escaped(client: TestClient) -> None:
    r = client.get("/events", params={"user": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


# --- Registration --------------------------------------------------------------------------


def test_register_for_event_without_logging_in(client: TestClient, connect: Connect) -> None:
    # BUG (S2): anyone can register anyone, via a GET.
    r = client.get("/register_event/1", params={"user": "victim"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/my-events?user=victim"
    assert connect().execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 1


def test_repeated_registration_creates_duplicate_rows(client: TestClient, connect: Connect) -> None:
    # BUG (A4): a double click or a retry registers twice.
    for _ in range(3):
        client.get("/register_event/2", params={"user": "asha"}, follow_redirects=False)
    n = (
        connect()
        .execute("SELECT COUNT(*) FROM registrations WHERE username='asha' AND event_id=2")
        .fetchone()[0]
    )
    assert n == 3
    r = client.get("/my-events", params={"user": "asha"})
    assert r.text.count("Dance Battle") == 3


def test_registration_for_a_missing_event_is_accepted(client: TestClient, connect: Connect) -> None:
    # BUG (A5): no foreign key and no existence check.
    r = client.get("/register_event/999", params={"user": "asha"}, follow_redirects=False)
    assert r.status_code == 302
    assert connect().execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 1
    assert "Proceed to Checkout" in client.get("/my-events", params={"user": "asha"}).text


def test_event_404_is_a_deliberate_crash(client: TestClient, connect: Connect) -> None:
    # Demo crash (A7): event_id 404 divides by zero; the global handler shows the exception text.
    r = client.get("/register_event/404", params={"user": "asha"})
    assert r.status_code == 500
    assert "division by zero" in r.text
    assert "Monolith Failure" in r.text
    assert connect().execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 0


def test_register_event_requires_user(client: TestClient) -> None:
    assert client.get("/register_event/1").status_code == 422


# --- My events and checkout ----------------------------------------------------------------


def test_my_events_lists_the_users_registrations(client: TestClient) -> None:
    client.get("/register_event/1", params={"user": "asha"})
    client.get("/register_event/3", params={"user": "ravi"})
    r = client.get("/my-events", params={"user": "asha"})
    assert "Hackathon" in r.text
    assert "AI Workshop" not in r.text


def test_my_events_empty_state(client: TestClient) -> None:
    r = client.get("/my-events", params={"user": "asha"})
    assert "No events registered yet" in r.text


def test_checkout_totals_every_event_not_the_users_registrations(client: TestClient) -> None:
    # BUG (A3): the total is the sum of all event fees, whoever is checking out.
    client.get("/register_event/2", params={"user": "asha"})
    r = client.get("/checkout")
    assert r.status_code == 200
    assert re.findall(r"₹ (\d+)", r.text) == ["1200"]  # 500 + 300 + 400


def test_checkout_forgets_who_is_logged_in(client: TestClient) -> None:
    # BUG (A3): checkout renders with user="" so the nav shows the logged-out state.
    r = client.get("/checkout")
    assert "Create Account" in r.text
    assert "Logged in as" not in r.text


def test_logout_is_just_a_link_to_login(client: TestClient) -> None:
    r = client.get("/events", params={"user": "asha"})
    assert 'href="/login"' in r.text and "Logout" in r.text


# --- Seed script ---------------------------------------------------------------------------


def test_insert_events_is_not_idempotent(tmp_path: Path) -> None:
    # BUG (A8): each run appends the nine events again, which is how fest.db got duplicates.
    shutil.copy(ROOT / "database.py", tmp_path / "database.py")
    shutil.copy(ROOT / "insert_events.py", tmp_path / "insert_events.py")
    for _ in range(2):
        subprocess.run(
            [sys.executable, "insert_events.py"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env={"PYTHONIOENCODING": "utf-8", "SYSTEMROOT": "C:\\Windows", "PATH": ""},
        )
    n = sqlite3.connect(tmp_path / "fest.db").execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert n == 18
