"""Shared fixtures: a real PostgreSQL database, a fresh schema per session, empty tables per test.

Start the database with ``docker compose up -d db`` (port 5440). CI points
``FESTRUSH_TEST_DATABASE_URL`` at its own service container.
"""

import os
import re
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import DictRow

from festrush.app import create_app
from festrush.config import REPO_ROOT, Settings
from festrush.db import migrate

TEST_DATABASE_URL = os.environ.get(
    "FESTRUSH_TEST_DATABASE_URL",
    "postgresql://festrush:festrush-dev@localhost:5440/festrush_test",
)
Db = psycopg.Connection[DictRow]
TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
PASSWORD = "correct-horse-battery"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "secret_key": "test-secret-key-0123456789",
        "insecure_fast_hashing": True,
        "pool_min_size": 1,
        "pool_max_size": 20,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _ensure_database() -> None:
    info = conninfo_to_dict(TEST_DATABASE_URL)
    name = str(info["dbname"])
    admin = make_conninfo(TEST_DATABASE_URL, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture(scope="session", autouse=True)
def schema() -> None:
    """Rebuild the schema from the migration files once per run."""
    _ensure_database()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    migrate(TEST_DATABASE_URL, REPO_ROOT / "db" / "migrations")


@pytest.fixture(autouse=True)
def clean_tables(schema: None) -> None:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE users, events, outbox, mock_charges, login_failures RESTART IDENTITY CASCADE")


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def db() -> Iterator[Db]:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True, row_factory=psycopg.rows.dict_row) as c:
        yield c


def csrf_token(client: TestClient, path: str = "/login") -> str:
    match = TOKEN_RE.search(client.get(path).text)
    assert match, f"no CSRF token on {path}"
    return match.group(1)


def post(
    client: TestClient,
    path: str,
    data: dict[str, str] | None = None,
    *,
    form: str = "/login",
    follow: bool = False,
    token: str | None = None,
) -> Any:
    """POST a form the way a browser does: with the CSRF token taken from a rendered page."""
    body = dict(data or {})
    body["csrf_token"] = token if token is not None else csrf_token(client, form)
    return client.post(path, data=body, follow_redirects=follow)


def signup(client: TestClient, username: str = "asha", password: str = PASSWORD) -> Any:
    return post(client, "/register", {"username": username, "password": password}, form="/register")


def login(client: TestClient, username: str = "asha", password: str = PASSWORD) -> Any:
    return post(client, "/login", {"username": username, "password": password})


@pytest.fixture
def make_event(db: Db) -> Any:
    def _make(name: str = "Hackathon", fee: int = 500, capacity: int = 3) -> int:
        row = db.execute(
            "INSERT INTO events (name, fee, capacity) VALUES (%s, %s, %s) RETURNING id",
            (name, fee, capacity),
        ).fetchone()
        assert row
        return int(row["id"])

    return _make


@pytest.fixture
def user_client(client: TestClient) -> TestClient:
    """A client that is signed in as ``asha``."""
    assert signup(client).status_code == 303
    assert login(client).status_code == 303
    return client


@pytest.fixture
def make_user(db: Db) -> Any:
    """Insert a user directly (no hashing) and return its id."""

    def _make(name: str = "student") -> int:
        row = db.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, 'x') RETURNING id", (name,)
        ).fetchone()
        assert row
        return int(row["id"])

    return _make


@pytest.fixture
def make_token(db: Db) -> Any:
    """Mint a session token for a user, the way the load tests do."""
    from festrush.security import hash_token

    def _make(user_id: int) -> str:
        token = f"token-{user_id}-{uuid.uuid4().hex}"
        db.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at)"
            " VALUES (%s, %s, now() + interval '1 hour')",
            (hash_token(token), user_id),
        )
        return token

    return _make


@pytest.fixture
def app_state(client: TestClient) -> Any:
    """The running app's pool, provider and settings."""
    return client.app.state.festrush  # type: ignore[attr-defined]
