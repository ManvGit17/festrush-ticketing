"""Where does the time go when a student reserves a seat? No HTTP, no load generator.

Times the pieces of the reserve request directly against PostgreSQL, one at a time and then
from many threads, so the cost of the database work can be told apart from the web stack.

    docker compose --profile bench run --rm --entrypoint python bench bench/profile_reserve.py

It builds its own tiny dataset in the benchmark database and leaves it behind.
"""

import argparse
import hashlib
import os
import statistics
import threading
import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.rows import dict_row

from festrush import inventory, services
from festrush.config import REPO_ROOT, Settings
from festrush.db import Conn, make_pool, migrate
from festrush.idempotency import request_hash, run_idempotent

URL = os.environ.get(
    "FESTRUSH_BENCH_DATABASE_URL", "postgresql://festrush:festrush-dev@localhost:5440/festrush_bench"
)


def prepare(users: int, seats: int) -> list[str]:
    migrate(URL, REPO_ROOT / "db" / "migrations")
    with psycopg.connect(URL, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("TRUNCATE users, events, outbox, mock_charges, login_failures RESTART IDENTITY CASCADE")
        conn.execute("INSERT INTO events (name, fee, capacity) VALUES ('Profile', 100, %s)", (seats,))
        conn.execute(
            "INSERT INTO users (username, password_hash)"
            " SELECT 'prof' || lpad(n::text, 6, '0'), 'x' FROM generate_series(1, %s) n",
            (users,),
        )
        ids = [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]
        tokens = [f"prof-token-{i}" for i in range(len(ids))]
        for token, uid in zip(tokens, ids, strict=True):
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, now() + interval '1 day')",
                (hashlib.sha256(token.encode()).digest(), uid),
            )
    return tokens


def timed(label: str, n: int, fn: Callable[[int], Any]) -> None:
    samples = []
    for i in range(n):
        t = time.perf_counter()
        fn(i)
        samples.append((time.perf_counter() - t) * 1000)
    print(
        f"  {label:<46} median {statistics.median(samples):6.2f} ms   p95 {sorted(samples)[int(n * 0.95)]:6.2f} ms"
    )


def throughput(label: str, threads: int, per_thread: int, fn: Callable[[int], Any]) -> None:
    gate = threading.Barrier(threads + 1)

    def run(t: int) -> None:
        gate.wait()
        for i in range(per_thread):
            fn(t * per_thread + i)

    ts = [threading.Thread(target=run, args=(t,)) for t in range(threads)]
    for t in ts:
        t.start()
    gate.wait()
    start = time.perf_counter()
    for t in ts:
        t.join()
    took = time.perf_counter() - start
    print(f"  {label:<46} {threads * per_thread / took:8.0f} operations/s with {threads} threads")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=300)
    args = p.parse_args()
    users = max(args.n * 4, 1400)
    settings = Settings(
        _env_file=None,
        database_url=URL,
        secret_key="profile-secret-key-000",
        inventory_strategy="atomic_update",
    )
    for mode in ("on", "off"):
        with psycopg.connect(URL, autocommit=True) as c:
            c.execute(f"ALTER DATABASE {psycopg.connect(URL).info.dbname} SET synchronous_commit = {mode}")
        tokens = prepare(users, seats=users)  # capacity never runs out: this times the work, not the sell-out
        pool = make_pool(URL, 4, 40)
        print(f"\nsynchronous_commit = {mode}")

        def lookup(i: int) -> None:
            with pool.connection() as conn:
                services.user_for_session(conn, tokens[i])

        def reserve_only(i: int) -> None:
            with pool.connection() as conn:
                inventory.reserve(conn, settings, i + 1, 1)

        def reserve_keyed(i: int) -> None:
            def work(conn: Conn) -> tuple[int, dict[str, Any]]:
                r = inventory.reserve(conn, settings, i + 1, 1)
                return 201, {"outcome": r.outcome.value}

            run_idempotent(pool, i + 1, f"k{mode}-{i}", request_hash("r", 1), work)

        def whole_request(i: int) -> None:
            lookup(i)
            reserve_keyed(i)

        timed("session lookup (read only)", args.n, lookup)
        timed("reserve, no idempotency key", args.n, lambda i: reserve_only(args.n + i))
        timed("reserve inside an idempotency key", args.n, lambda i: reserve_keyed(2 * args.n + i))
        timed("whole request: lookup + keyed reserve", args.n, lambda i: whole_request(3 * args.n + i))

        prepare(users, seats=users)
        throughput("session lookup only", 32, 40, lookup)
        throughput("reserve, no key (distinct users, one event)", 32, 40, lambda i: reserve_only(i))
        prepare(users, seats=users)
        throughput("whole request, 32 threads", 32, 40, whole_request)
        pool.close()


if __name__ == "__main__":
    main()
