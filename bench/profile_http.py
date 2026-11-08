"""Where does the Python CPU go in a reserve request? In-process, through the real FastAPI app.

    docker compose --profile bench run --rm --entrypoint python bench bench/profile_http.py

Uses Starlette's test client (no network, no load generator), one request at a time, and prints
the time per request and the heaviest functions. It leaves a small dataset in the benchmark
database.
"""

import cProfile
import hashlib
import os
import pstats
import time

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from festrush.app import create_app
from festrush.config import REPO_ROOT, Settings
from festrush.db import migrate

URL = os.environ.get(
    "FESTRUSH_BENCH_DATABASE_URL", "postgresql://festrush:festrush-dev@localhost:5440/festrush_bench"
)
N = 600


def main() -> None:
    migrate(URL, REPO_ROOT / "db" / "migrations")
    with psycopg.connect(URL, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("TRUNCATE users, events, outbox, mock_charges, login_failures RESTART IDENTITY CASCADE")
        conn.execute("INSERT INTO events (name, fee, capacity) VALUES ('Profile', 100, %s)", (N * 2,))
        conn.execute(
            "INSERT INTO users (username, password_hash)"
            " SELECT 'prof' || lpad(n::text, 6, '0'), 'x' FROM generate_series(1, %s) n",
            (N,),
        )
        ids = [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]
        for i, uid in enumerate(ids):
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, now() + interval '1 day')",
                (hashlib.sha256(f"prof-token-{i}".encode()).digest(), uid),
            )
    settings = Settings(
        _env_file=None,
        database_url=URL,
        secret_key="profile-secret-key-000",
        pool_min_size=2,
        pool_max_size=4,
    )
    with TestClient(create_app(settings)) as client:
        for i in range(30):  # warm up connections and imports
            client.get("/api/events/1")
        profiler = cProfile.Profile()
        start = time.perf_counter()
        profiler.enable()
        for i in range(N):
            client.post(
                "/api/events/1/reserve",
                headers={"Authorization": f"Bearer prof-token-{i}", "Idempotency-Key": f"p{i}"},
            )
        profiler.disable()
        took = time.perf_counter() - start
    print(f"{N} reserve requests, one at a time: {took / N * 1000:.2f} ms each (profiling adds overhead)\n")
    stats = pstats.Stats(profiler).sort_stats("cumulative")
    stats.print_stats(28)


if __name__ == "__main__":
    main()
