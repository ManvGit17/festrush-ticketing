"""Run one load scenario end to end and write the result as JSON.

    uv run python bench/run.py --strategy atomic_update --users 5000 --seats 200

What it does, in order:

 1. Creates (or empties) a dedicated benchmark database and applies the migrations.
 2. Inserts one event with the given number of seats, and one student per simulated user, each
    with a ready-made session token (so password hashing is not part of the measurement).
 3. Starts the web app (several processes) and the background worker as real subprocesses.
 4. Runs Locust headless with the chosen scenario.
 5. Lets the worker drain the outbox and settle payments, then stops everything.
 6. Runs the invariant checks against the database and records the Locust statistics.

The result goes to ``bench/results/<label>.json``. Exit status is 1 if the outcome is not the
one expected: safe strategies must have zero oversells and no violated invariant; ``--expect-oversell``
inverts that for the naive baseline.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from festrush import invariants
from festrush.config import REPO_ROOT
from festrush.db import migrate

DEFAULT_URL = "postgresql://festrush:festrush-dev@localhost:5440/festrush_bench"
RESULTS = REPO_ROOT / "bench" / "results"
FEE = 100


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strategy", choices=["naive", "row_lock", "atomic_update", "skip_locked"], required=True)
    p.add_argument("--scenario", choices=["spike", "retry-storm", "soak"], default="spike")
    p.add_argument("--users", type=int, default=300, help="simulated students")
    p.add_argument("--seats", type=int, default=30, help="seats at the event (soak: per event)")
    p.add_argument("--events", type=int, default=1, help="number of events (soak only)")
    p.add_argument("--duration", type=int, default=60, help="seconds (soak only)")
    p.add_argument("--web-workers", type=int, default=4, help="web server processes")
    p.add_argument("--pool-size", type=int, default=10, help="database connections per web process")
    p.add_argument("--spawn-rate", type=int, default=0, help="users started per second (0 = all at once)")
    p.add_argument("--naive-delay-ms", type=int, default=0, help="widen the naive strategy's race window")
    p.add_argument("--mockpay-latency-ms", type=int, default=0)
    p.add_argument("--mockpay-decline-rate", type=float, default=0.0)
    p.add_argument("--mockpay-timeout-rate", type=float, default=0.0)
    p.add_argument("--hold-minutes", type=int, default=10)
    p.add_argument(
        "--synchronous-commit",
        choices=["on", "off"],
        default="on",
        help="on: every commit waits for the disk (durable); off: simulates a fast disk so lock waits, not fsync, dominate",
    )
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--database-url", default=os.environ.get("FESTRUSH_BENCH_DATABASE_URL", DEFAULT_URL))
    p.add_argument("--label", default="", help="result file name (default: derived from the options)")
    p.add_argument("--expect-oversell", action="store_true", help="succeed only if overselling happened")
    p.add_argument("--out", type=Path, default=RESULTS)
    return p.parse_args()


# --- Setup ----------------------------------------------------------------------------------


def prepare_database(args: argparse.Namespace) -> None:
    info = conninfo_to_dict(args.database_url)
    admin = make_conninfo(args.database_url, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (info["dbname"],)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(str(info["dbname"]))))
    migrate(args.database_url, REPO_ROOT / "db" / "migrations")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("ALTER DATABASE {} SET synchronous_commit = {}").format(
                sql.Identifier(str(info["dbname"])), sql.Literal(args.synchronous_commit)
            )
        )

    events = args.events if args.scenario == "soak" else 1
    with psycopg.connect(args.database_url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("TRUNCATE users, events, outbox, mock_charges, login_failures RESTART IDENTITY CASCADE")
        for i in range(events):
            conn.execute(
                "INSERT INTO events (name, fee, capacity) VALUES (%s, %s, %s)",
                (f"Bench event {i + 1}", FEE, args.seats),
            )
        # One cheap placeholder hash for every user: nobody logs in with a password here.
        conn.execute(
            "INSERT INTO users (username, password_hash)"
            " SELECT 'bench' || lpad(n::text, 6, '0'), 'bench-placeholder' FROM generate_series(1, %s) n",
            (args.users,),
        )
        ids = [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]
        rows = [(hashlib.sha256(f"bench-token-{i}".encode()).digest(), uid) for i, uid in enumerate(ids)]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, now() + interval '1 day')",
                rows,
            )
        conn.execute("ANALYZE")


def child_env(args: argparse.Namespace) -> dict[str, str]:
    return {
        **os.environ,
        "FESTRUSH_DATABASE_URL": args.database_url,
        "FESTRUSH_SECRET_KEY": "benchmark-secret-key-0000",
        "FESTRUSH_INVENTORY_STRATEGY": args.strategy,
        "FESTRUSH_NAIVE_DELAY_MS": str(args.naive_delay_ms),
        "FESTRUSH_POOL_MIN_SIZE": "2",
        "FESTRUSH_POOL_MAX_SIZE": str(args.pool_size),
        "FESTRUSH_HOLD_MINUTES": str(args.hold_minutes),
        "FESTRUSH_PAYMENT_RECONCILE_SECONDS": "5" if args.scenario == "soak" else "30",
        "FESTRUSH_PAYMENT_GRACE_SECONDS": "10" if args.scenario == "soak" else "120",
        "FESTRUSH_MOCKPAY_LATENCY_MS": str(args.mockpay_latency_ms),
        "FESTRUSH_MOCKPAY_DECLINE_RATE": str(args.mockpay_decline_rate),
        "FESTRUSH_MOCKPAY_TIMEOUT_RATE": str(args.mockpay_timeout_rate),
        "PYTHONUNBUFFERED": "1",
    }


def stop(proc: subprocess.Popen[Any]) -> None:
    """Stop a process and everything it started (the web server runs worker processes)."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_until_up(port: int, proc: subprocess.Popen[Any]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit("the web server exited during startup")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise SystemExit("the web server did not become ready in 60 seconds")


# --- Measuring --------------------------------------------------------------------------------


def settle(args: argparse.Namespace, deadline_s: int = 90) -> bool:
    """Wait for the worker to deliver every message and settle every payment."""
    deadline = time.monotonic() + deadline_s
    with psycopg.connect(args.database_url, autocommit=True, row_factory=dict_row) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                "SELECT (SELECT count(*) FROM outbox WHERE processed_at IS NULL AND dead_at IS NULL) AS outbox,"
                " (SELECT count(*) FROM payments WHERE status IN ('initiated', 'processing', 'unknown')) AS unsettled,"
                " (SELECT count(*) FROM reservations r JOIN orders o ON o.id = r.order_id"
                "   WHERE o.status = 'pending' AND r.status = 'held') AS pending_holds"
            ).fetchone()
            assert row is not None
            if args.scenario != "soak" and row["outbox"] == 0 and row["unsettled"] == 0:
                return True
            if (
                args.scenario == "soak"
                and row["outbox"] == 0
                and row["unsettled"] == 0
                and row["pending_holds"] == 0
            ):
                return True
            time.sleep(0.5)
    return False


def read_database(args: argparse.Namespace) -> dict[str, Any]:
    with psycopg.connect(args.database_url, autocommit=True, row_factory=dict_row) as conn:
        by = {
            table: {
                r["status"]: r["n"]
                for r in conn.execute(f"SELECT status, count(*) AS n FROM {table} GROUP BY 1")
            }
            for table in ("reservations", "orders", "payments")
        }
        mail = conn.execute("SELECT count(*) AS n FROM mailbox").fetchone()
        charges = conn.execute(
            "SELECT count(*) AS n, count(*) FILTER (WHERE refunded) AS refunded FROM mock_charges"
        ).fetchone()
        version = conn.execute("SHOW server_version").fetchone()
        found = invariants.violations(conn)
        return {
            "reservations": by["reservations"],
            "orders": by["orders"],
            "payments": by["payments"],
            "messages": mail["n"] if mail else 0,
            "charges": dict(charges) if charges else {},
            "oversold_seats": invariants.oversold_seats(conn),
            "violations": {name: len(rows) for name, rows in found.items()},
            "postgres": version["server_version"] if version else "?",
        }


def git_commit() -> str:
    if os.environ.get("BENCH_COMMIT", "unknown") != "unknown":
        return os.environ["BENCH_COMMIT"]
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    args = parse_args()
    label = args.label or f"{args.scenario}-{args.strategy}-{args.users}u-{args.seats}s"
    prepare_database(args)
    env = child_env(args)
    event_ids = ",".join(str(i) for i in range(1, (args.events if args.scenario == "soak" else 1) + 1))

    web = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "festrush",
            "serve",
            "--port",
            str(args.port),
            "--workers",
            str(args.web_workers),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    background = subprocess.Popen(
        [sys.executable, "-m", "festrush", "worker"], cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL
    )
    started = datetime.now(UTC)
    try:
        wait_until_up(args.port, web)
        with tempfile.TemporaryDirectory() as tmp:
            stats_file = Path(tmp) / "stats.json"
            # A spike that has not finished by now is stuck, not slow: fail it instead of waiting.
            cap = args.duration if args.scenario == "soak" else 120
            cmd = [
                sys.executable,
                "-m",
                "locust",
                "-f",
                str(REPO_ROOT / "bench" / "locustfile.py"),
                "--host",
                f"http://127.0.0.1:{args.port}",
                "--headless",
                "--users",
                str(args.users),
                "--spawn-rate",
                str(args.spawn_rate or args.users),
                "--run-time",
                f"{cap}s",
                "--only-summary",
            ]
            t0 = time.monotonic()
            locust = subprocess.run(
                cmd,
                cwd=REPO_ROOT,
                env={
                    **env,
                    "BENCH_SCENARIO": args.scenario,
                    "BENCH_USERS": str(args.users),
                    "BENCH_EVENT_IDS": event_ids,
                    "BENCH_STATS_OUT": str(stats_file),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            load_seconds = time.monotonic() - t0
            if not stats_file.exists():
                sys.stderr.write(locust.stdout[-3000:] + locust.stderr[-3000:])
                raise SystemExit("locust produced no statistics")
            locust_stats = json.loads(stats_file.read_text(encoding="utf-8"))
        settled = settle(args)
    finally:
        stop(background)
        stop(web)

    db = read_database(args)
    result = {
        "label": label,
        "when": started.isoformat(timespec="seconds"),
        "commit": git_commit(),
        "options": {
            k: str(v) if isinstance(v, Path) else v
            for k, v in vars(args).items()
            if k not in ("database_url", "out")
        },
        "machine": {
            "os": platform.platform(),
            "cpu": platform.processor() or platform.machine(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
            "postgres": db["postgres"],
        },
        "load_seconds": round(load_seconds, 2),
        "timed_out": args.scenario != "soak" and load_seconds >= 119,
        "settled": settled,
        "locust": locust_stats,
        "database": db,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{label}.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print_summary(result)

    ok = db["oversold_seats"] == 0 and not db["violations"] and settled and not result["timed_out"]
    if result["timed_out"]:
        print("  TIMED OUT: the run did not finish within 120 s")
    if args.expect_oversell:
        ok = db["oversold_seats"] > 0
    failed = locust_stats.get("Aggregated", {}).get("failures", 0)
    if failed and not args.expect_oversell:
        print(f"  {failed} requests failed (5xx, timeouts or unexpected statuses)")
        ok = False
    return 0 if ok else 1


def print_summary(r: dict[str, Any]) -> None:
    db, agg = r["database"], r["locust"].get("Aggregated", {})
    reserve = r["locust"].get("POST /api/events/[id]/reserve", {})
    res = db["reservations"]
    print(
        f"\n{r['label']}  ({r['options']['users']} users, {r['options']['seats']} seats, {r['options']['strategy']})"
    )
    print(
        f"  reserve: {reserve.get('requests', 0)} requests, p50 {reserve.get('p50_ms')} ms, "
        f"p95 {reserve.get('p95_ms')} ms, p99 {reserve.get('p99_ms')} ms, {reserve.get('rps')} req/s"
    )
    print(
        f"  all requests: {agg.get('requests', 0)}, failures {agg.get('failures', 0)}, in {r['load_seconds']} s"
    )
    print(
        f"  reservations {res}  orders {db['orders']}  payments {db['payments']}  messages {db['messages']}"
    )
    print(f"  OVERSOLD SEATS: {db['oversold_seats']}   violated invariants: {db['violations'] or 'none'}")


if __name__ == "__main__":
    sys.exit(main())
