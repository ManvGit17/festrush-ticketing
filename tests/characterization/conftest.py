"""Fixtures for the characterization suite.

These tests pin the behaviour of the original monolith (v1, commit fe966d8) exactly as it is,
bugs included, so every later change is deliberate. A test marked ``BUG`` documents a defect
from docs/audit.md; when a phase fixes that defect, the test flips in the same commit.

The app hard-codes ``sqlite3.connect("fest.db")`` and imports ``get_db`` by name, so each test
points those names at a throwaway database instead of touching the committed file.
"""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]

SEED_EVENTS = [
    ("Hackathon", 500),
    ("Dance Battle", 300),
    ("AI Workshop", 400),
]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "fest.db"


@pytest.fixture
def connect(db_path: Path) -> Callable[[], sqlite3.Connection]:
    def _connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return _connect


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, connect: Callable[[], sqlite3.Connection]
) -> Iterator[TestClient]:
    # Templates are loaded from a path relative to the working directory.
    monkeypatch.chdir(ROOT)
    monkeypatch.syspath_prepend(str(ROOT))

    import checkout
    import main

    monkeypatch.setattr(main, "get_db", connect)
    monkeypatch.setattr(checkout, "get_db", connect)

    with TestClient(main.app, raise_server_exceptions=False) as c:
        db = connect()
        db.executemany("INSERT INTO events (name, fee) VALUES (?, ?)", SEED_EVENTS)
        db.commit()
        db.close()
        yield c
