"""Connection pool and the migration runner."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

Conn = psycopg.Connection[DictRow]
Pool = ConnectionPool[Conn]

# Arbitrary constant: only one process applies migrations at a time.
_MIGRATION_LOCK = 7_261_998_401


def make_pool(database_url: str, min_size: int, max_size: int) -> Pool:
    pool: Pool = ConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        # connect_timeout: while PostgreSQL is down or restarting, a connection attempt must fail
        # fast so the pool can try again, not hang for minutes.
        kwargs={"row_factory": dict_row, "connect_timeout": 3},
        open=False,
        # After PostgreSQL restarts, connections in the pool are dead. Test each one as it is
        # handed out (and replace it) instead of failing the request that happens to get it.
        check=ConnectionPool.check_connection,
        # Waiting for a connection longer than this means the database is not coming back soon:
        # answer 503 so the client can retry, instead of hanging.
        timeout=5.0,
        reconnect_timeout=300.0,
        max_lifetime=1800.0,
    )
    pool.open(wait=True, timeout=30)
    return pool


@contextmanager
def connect(database_url: str) -> Iterator[Conn]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        yield conn


def migration_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def migrate(database_url: str, directory: Path) -> list[str]:
    """Apply every migration that has not run yet, each in its own transaction.

    Migrations are append-only: a file that has been applied is never re-run or edited.
    Returns the versions applied by this call.
    """
    applied_now: list[str] = []
    with connect(database_url) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.commit()
            done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
            for path in migration_files(directory):
                version = path.stem
                if version in done:
                    continue
                log.info("applying migration %s", version)
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                applied_now.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
            conn.commit()
    return applied_now


def one(conn: Conn, query: str, params: Any = None) -> dict[str, Any] | None:
    return conn.execute(query, params).fetchone()
