"""Migrations, seeding, the CLI and the root entry point."""

import importlib
import re
import sys
from typing import Any

import pytest

from festrush import cli, services
from festrush.config import REPO_ROOT
from festrush.db import migrate, migration_files
from tests.conftest import TEST_DATABASE_URL as TEST_URL
from tests.conftest import Db


def test_migrations_are_idempotent() -> None:
    assert migrate(TEST_URL, REPO_ROOT / "db" / "migrations") == []


def test_migration_files_are_numbered_and_sorted() -> None:
    names = [p.name for p in migration_files(REPO_ROOT / "db" / "migrations")]
    assert names == sorted(names)
    assert all(n[:4].isdigit() for n in names)


def test_seeding_twice_does_not_duplicate_events(db: Db) -> None:
    # A8: v1's seed script appended nine more events on every run.
    for _ in range(3):
        services.seed_events(db)
    row = db.execute("SELECT count(*) AS n FROM events").fetchone()
    assert row
    assert row["n"] == len(services.SEED_EVENTS)


def test_reseeding_never_lowers_capacity_below_claimed_seats(db: Db, make_event: Any) -> None:
    db.execute("INSERT INTO events (name, fee, capacity, claimed) VALUES ('Hackathon', 1, 200, 180)")
    services.seed_events(db)
    row = db.execute("SELECT capacity, claimed FROM events WHERE name = 'Hackathon'").fetchone()
    assert row
    assert row["capacity"] >= row["claimed"]


def test_seed_command_creates_demo_accounts_once(
    db: Db, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.conftest import make_settings

    settings = make_settings()
    cli.cmd_seed(settings, 3)
    cli.cmd_seed(settings, 3)
    out = capsys.readouterr().out
    assert "3 new demo account(s)" in out
    assert "0 new demo account(s)" in out
    row = db.execute("SELECT count(*) AS n FROM users").fetchone()
    assert row
    assert row["n"] == 3


def test_the_root_entry_point_exposes_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """``uvicorn main:app`` keeps working from the project root, as in v1."""
    from festrush.config import get_settings

    monkeypatch.setenv("FESTRUSH_SECRET_KEY", "test-secret-key-0123456789")
    monkeypatch.setenv("FESTRUSH_DATABASE_URL", TEST_URL)
    get_settings.cache_clear()
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    assert hasattr(main, "app")
    get_settings.cache_clear()
    sys.modules.pop("main", None)


def test_no_student_identifier_is_hardcoded_in_the_source() -> None:
    """v1 carried a student registration number as a constant that nothing used (audit S3)."""
    srn = re.compile(r"\bPES\d[A-Z]{2}\d{2}[A-Z]{2}\d{3}\b")
    roots = [REPO_ROOT / "src", REPO_ROOT / "legacy", REPO_ROOT / "tests", REPO_ROOT / "locust"]
    files = [REPO_ROOT / "main.py", *(f for r in roots for f in r.rglob("*") if f.suffix in {".py", ".html"})]
    assert [str(f) for f in files if srn.search(f.read_text(encoding="utf-8"))] == []


def test_the_secret_key_is_required() -> None:
    from pydantic import ValidationError

    from festrush.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
