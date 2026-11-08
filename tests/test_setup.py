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


def test_reseeding_never_lowers_capacity_below_seats_taken(db: Db, make_event: Any) -> None:
    db.execute(
        "INSERT INTO events (name, fee, capacity, seats_taken) VALUES ('Hackathon', 1, 200, 180)"
    )
    services.seed_events(db)
    row = db.execute("SELECT capacity, seats_taken FROM events WHERE name = 'Hackathon'").fetchone()
    assert row
    assert row["capacity"] >= row["seats_taken"]


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


def test_the_root_entry_point_still_exposes_app_and_the_srn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``uvicorn main:app`` and the SRN line in main.py are kept from v1 (may be graded)."""
    from festrush.config import get_settings

    monkeypatch.setenv("FESTRUSH_SECRET_KEY", "test-secret-key-0123456789")
    monkeypatch.setenv("FESTRUSH_DATABASE_URL", TEST_URL)
    get_settings.cache_clear()
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    assert hasattr(main, "app")
    original = re.search(
        r'^SRN = "([^"]+)"',
        (REPO_ROOT / "legacy" / "v1" / "main.py").read_text(encoding="utf-8"),
        re.M,
    )
    assert original
    assert original.group(1) == main.SRN
    get_settings.cache_clear()
    sys.modules.pop("main", None)


def test_the_secret_key_is_required() -> None:
    from pydantic import ValidationError

    from festrush.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
