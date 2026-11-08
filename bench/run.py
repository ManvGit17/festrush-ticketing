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
import threading
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
PAYMENTS_PORT = 8071


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strategy", choices=["naive", "row_lock", "atomic_update", "skip_locked"], required=True)
    p.add_argument("--scenario", choices=["spike", "retry-storm", "soak"], default="spike")
    p.add_argument("--users", type=int, default=300, help="simulated students")
    p.add_argument(
        "--driver",
        choices=["locust", "k6"],
        default="locust",
        help="load generator. k6 is a compiled tool with a much higher ceiling than Locust",
    )
    p.add_argument("--seats", type=int, default=30, help="seats at the event (soak: per event)")
    p.add_argument("--events", type=int, default=1, help="number of events (soak only)")
    p.add_argument("--duration", type=int, default=60, help="seconds (soak only)")
    p.add_argument("--web-workers", type=int, default=4, help="web server processes")
    p.add_argument("--pool-size", type=int, default=10, help="database connections per web process")
    p.add_argument("--threads", type=int, default=40, help="handler threads per web process")
    p.add_argument("--spawn-rate", type=int, default=0, help="users started per second (0 = all at once)")
    p.add_argument("--naive-delay-ms", type=int, default=0, help="widen the naive strategy's race window")
    p.add_argument("--mockpay-latency-ms", type=int, default=0)
    p.add_argument("--mockpay-decline-rate", type=float, default=0.0)
    p.add_argument("--mockpay-timeout-rate", type=float, default=0.0)
    p.add_argument("--hold-minutes", type=int, default=10)
    p.add_argument("--notify-inline", action="store_true", help="send confirmations inside the pay request")
    p.add_argument("--email-delay-ms", type=int, default=0, help="simulated latency of sending one message")
    p.add_argument(
        "--payments-service",
        action="store_true",
        help="run the payment provider as its own process (the app talks to it over HTTP)",
    )
    p.add_argument(
        "--chaos",
        choices=["none", "kill-worker", "kill-payments", "restart-db"],
        default="none",
        help="break something while the load runs, then put it back",
    )
    p.add_argument("--chaos-at", type=float, default=2.0, help="seconds after the load starts")
    p.add_argument("--chaos-for", type=float, default=6.0, help="seconds the fault lasts")
    p.add_argument(
        "--refreshes", type=int, default=0, help="page refreshes (GET event) each student does first"
    )
    p.add_argument("--gateway", action="store_true", help="put the nginx gateway in front of the app")
    p.add_argument("--claim-rate", type=int, default=300, help="gateway: seat claims per second let through")
    p.add_argument("--claim-burst", type=int, default=200, help="gateway: extra claims allowed at once")
    p.add_argument("--queue-rate", type=int, default=600, help="gateway: queue joins and polls per second")
    p.add_argument("--catalog-ttl", type=int, default=1, help="gateway: seconds to cache event reads")
    p.add_argument("--gateway-port", type=int, default=8091)
    p.add_argument("--waiting-room", action="store_true", help="students join the queue before reserving")
    p.add_argument("--admit-per-second", type=int, default=150, help="waiting room admission rate")
    p.add_argument("--queue-mode", choices=["fifo", "lottery"], default="fifo")
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
        "FESTRUSH_THREADPOOL_SIZE": str(args.threads),
        "FESTRUSH_NOTIFY_INLINE": "true" if args.notify_inline else "false",
        "FESTRUSH_WORKER_HANDLER_DELAY_MS": str(args.email_delay_ms),
        "FESTRUSH_WAITING_ROOM_ENABLED": "true" if args.waiting_room else "false",
        "FESTRUSH_WAITING_ROOM_MODE": args.queue_mode,
        "FESTRUSH_WAITING_ROOM_ADMIT_PER_SECOND": str(args.admit_per_second),
        "FESTRUSH_POOL_MIN_SIZE": "2",
        "FESTRUSH_POOL_MAX_SIZE": str(args.pool_size),
        "FESTRUSH_HOLD_MINUTES": str(args.hold_minutes),
        "FESTRUSH_PAYMENT_RECONCILE_SECONDS": "5"
        if (args.scenario == "soak" or args.chaos != "none")
        else "30",
        "FESTRUSH_PAYMENT_GRACE_SECONDS": "10" if args.scenario == "soak" else "120",
        "FESTRUSH_PAYMENTS_URL": f"http://127.0.0.1:{PAYMENTS_PORT}" if args.payments_service else "",
        "FESTRUSH_PAYMENTS_TIMEOUT_SECONDS": "2",
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


def start_gateway(args: argparse.Namespace) -> subprocess.Popen[Any]:
    """Start nginx from the same template docker compose uses, pointed at the benchmark app."""
    text = (REPO_ROOT / "gateway" / "nginx.conf.template").read_text(encoding="utf-8")
    for name, value in {
        "UPSTREAM": f"127.0.0.1:{args.port}",
        "LISTEN_PORT": str(args.gateway_port),
        "CLAIM_RATE": str(args.claim_rate),
        "CLAIM_BURST": str(args.claim_burst),
        "QUEUE_RATE": str(args.queue_rate),
        "QUEUE_BURST": str(args.queue_rate),
        "LOGIN_RATE": "100000",
        "LOGIN_BURST": "100000",
        "CATALOG_TTL": str(args.catalog_ttl),
    }.items():
        text = text.replace("${" + name + "}", value)
    conf = Path(tempfile.mkdtemp()) / "nginx.conf"
    conf.write_text(text, encoding="utf-8")
    return subprocess.Popen(["nginx", "-c", str(conf), "-g", "daemon off;"], stdout=subprocess.DEVNULL)


def start_worker(env: dict[str, str]) -> subprocess.Popen[Any]:
    return subprocess.Popen(
        [sys.executable, "-m", "festrush", "worker"], cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL
    )


def start_payments(env: dict[str, str]) -> subprocess.Popen[Any]:
    return subprocess.Popen(
        [sys.executable, "-m", "festrush", "payments", "--port", str(PAYMENTS_PORT)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class Chaos:
    """Breaks one thing at a set moment, then puts it back. Everything it does is logged."""

    def __init__(self, args: argparse.Namespace, env: dict[str, str], procs: dict[str, Any]) -> None:
        self.args, self.env, self.procs = args, env, procs
        self.log: list[str] = []
        self.timers: list[threading.Timer] = []

    def _note(self, text: str) -> None:
        self.log.append(text)
        print(f"  [chaos] {text}", flush=True)

    def start(self) -> None:
        kind, at, last = self.args.chaos, self.args.chaos_at, self.args.chaos_for
        if kind == "none":
            return
        actions = {
            "kill-worker": (self._kill("background"), self._revive("background", start_worker)),
            "kill-payments": (self._kill("payments"), self._revive("payments", start_payments)),
            "restart-db": (self._stop_db, self._start_db),
        }
        down, up = actions[kind]
        self.timers = [threading.Timer(at, down), threading.Timer(at + last, up)]
        for t in self.timers:
            t.daemon = True
            t.start()

    def _kill(self, name: str):  # type: ignore[no-untyped-def]
        def go() -> None:
            self._note(f"killing {name}")
            stop(self.procs[name])

        return go

    def _revive(self, name: str, factory):  # type: ignore[no-untyped-def]
        def go() -> None:
            self._note(f"starting {name} again")
            self.procs[name] = factory(self.env)

        return go

    def _stop_db(self) -> None:
        self._note("stopping PostgreSQL")
        subprocess.run(["docker", "compose", "stop", "db"], cwd=REPO_ROOT, capture_output=True, check=False)

    def _start_db(self) -> None:
        self._note("starting PostgreSQL again")
        subprocess.run(["docker", "compose", "start", "db"], cwd=REPO_ROOT, capture_output=True, check=False)

    def cancel(self) -> None:
        for t in self.timers:
            t.cancel()

    def restore(self) -> None:
        """Put back anything still broken (the load may end before the fault does)."""
        self.cancel()
        kind = self.args.chaos
        if kind == "none" or not self.log or len(self.log) % 2 == 0:
            return  # nothing was broken, or it was already put back
        if kind == "restart-db":
            self._start_db()
        elif kind == "kill-worker":
            self._revive("background", start_worker)()
        elif kind == "kill-payments":
            self._revive("payments", start_payments)()


def probe_recovery(args: argparse.Namespace) -> float | None:
    """After the fault is over, how long until the app answers a real request again?"""
    started = time.monotonic()
    port = args.gateway_port if args.gateway else args.port
    while time.monotonic() - started < 90:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=3) as r:
                if r.status == 200:
                    return round(time.monotonic() - started, 1)
        except OSError:
            time.sleep(0.25)  # not up yet: look again shortly
            continue
        time.sleep(0.25)
    return None


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


K6_NAMES = {
    "reserve": "POST /api/events/[id]/reserve",
    "order": "POST /api/orders",
    "pay": "POST /api/orders/[id]/pay",
    "queue_join": "POST /api/queue/join",
    "queue_wait": "GET /api/queue/status",
    "refresh": "GET /api/events/[id] (refresh)",
}


def run_k6(
    args: argparse.Namespace, env: dict[str, str], event_ids: str, cap: int
) -> tuple[dict[str, Any], float]:
    """Run the spike with k6 and return statistics in the same shape the Locust path produces."""
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.json"
        cmd = [
            "k6",
            "run",
            "--quiet",
            "--summary-export",
            str(summary),
            str(REPO_ROOT / "bench" / "k6" / "spike.js"),
        ]
        k6_env = {
            **env,
            "TARGET": f"http://127.0.0.1:{args.gateway_port if args.gateway else args.port}",
            "USERS": str(args.users),
            "SCENARIO": args.scenario,
            "WAITING_ROOM": "1" if args.waiting_room else "0",
            "MAX_DURATION": f"{cap}s",
            "REFRESHES": str(args.refreshes),
            "EVENT_ID": event_ids.split(",")[0],
        }
        t0 = time.monotonic()
        proc = subprocess.run(cmd, cwd=REPO_ROOT, env=k6_env, capture_output=True, text=True, check=False)
        seconds = time.monotonic() - t0
        if not summary.exists():
            sys.stderr.write(proc.stdout[-3000:] + proc.stderr[-3000:])
            raise SystemExit("k6 produced no summary")
        metrics = json.loads(summary.read_text(encoding="utf-8"))["metrics"]

    def values(name: str) -> dict[str, Any]:
        m = metrics.get(name, {})
        return m.get("values", m)

    stats: dict[str, Any] = {}
    total = failures = 0
    for tag, label in K6_NAMES.items():
        count = int(values(f"http_reqs{{name:{tag}}}").get("count", 0))
        if not count:
            continue
        d = values(f"http_req_duration{{name:{tag}}}")
        stats[label] = {
            "requests": count,
            "failures": 0,
            "avg_ms": round(d.get("avg", 0), 1),
            "p50_ms": round(d.get("med", 0), 1),
            "p95_ms": round(d.get("p(95)", 0), 1),
            "p99_ms": round(d.get("p(99)", 0), 1),
            "max_ms": round(d.get("max", 0), 1),
            "rps": round(count / seconds, 1),
        }
        total += count
    failed_rate = values("http_req_failed").get("rate", values("http_req_failed").get("value", 0))
    failures = round(failed_rate * int(values("http_reqs").get("count", total)))
    allreqs = int(values("http_reqs").get("count", total))
    d = values("http_req_duration")
    stats["Aggregated"] = {
        "requests": allreqs,
        "failures": failures,
        "avg_ms": round(d.get("avg", 0), 1),
        "p50_ms": round(d.get("med", 0), 1),
        "p95_ms": round(d.get("p(95)", 0), 1),
        "p99_ms": round(d.get("p(99)", 0), 1),
        "max_ms": round(d.get("max", 0), 1),
        "rps": round(allreqs / seconds, 1),
    }
    stats["_errors"] = {}
    journey = values("reserve_journey_ms")
    stats["_extra"] = {
        "shed_429": int(values("shed_429").get("count", 0)),
        "reserve_journey_p50_ms": round(journey.get("med", 0), 1),
        "reserve_journey_p95_ms": round(journey.get("p(95)", 0), 1),
    }
    return stats, seconds


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

    web_log = open(os.environ["BENCH_WEB_LOG"], "w") if os.environ.get("BENCH_WEB_LOG") else None  # noqa: SIM115
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
        # BENCH_WEB_LOG=<file> keeps the web server's output, for working out why a run failed.
        stdout=web_log or subprocess.DEVNULL,
        stderr=subprocess.STDOUT if web_log else subprocess.DEVNULL,
    )
    background = subprocess.Popen(
        [sys.executable, "-m", "festrush", "worker"], cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL
    )
    payments = start_payments(env) if args.payments_service else None
    procs: dict[str, Any] = {"background": background, "payments": payments}
    chaos = Chaos(args, env, procs)
    started = datetime.now(UTC)
    gateway = None
    try:
        wait_until_up(args.port, web)
        if args.gateway:
            gateway = start_gateway(args)
            wait_until_up(args.gateway_port, gateway)
        cap = args.duration if args.scenario == "soak" else 120
        chaos.start()
        if args.driver == "k6":
            locust_stats, load_seconds = run_k6(args, env, event_ids, cap)
        else:
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
                    f"http://127.0.0.1:{args.gateway_port if args.gateway else args.port}",
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
                        "BENCH_RETRY_ERRORS": "1" if args.chaos != "none" else "0",
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
        chaos.restore()
        recovery_seconds = probe_recovery(args) if args.chaos != "none" else None
        settled = settle(args, 150 if args.chaos != "none" else 90)
    finally:
        if web_log is not None:
            web_log.close()
        chaos.cancel()
        if gateway is not None:
            stop(gateway)
        for proc in (procs["background"], procs["payments"], web):
            if proc is not None:
                stop(proc)

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
        "chaos": {"kind": args.chaos, "events": chaos.log, "recovery_seconds": recovery_seconds},
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
    # A database restart is allowed to fail the requests that arrive while it is down.
    if failed and not args.expect_oversell and args.chaos != "restart-db":
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
    extra = r["locust"].get("_extra")
    if extra:
        print(
            f"  student view of reserve (first attempt to answer): p50 {extra['reserve_journey_p50_ms']} ms, "
            f"p95 {extra['reserve_journey_p95_ms']} ms; shed with 429: {extra['shed_429']}"
        )
    ch = r.get("chaos", {})
    if ch.get("kind", "none") != "none":
        print(
            f"  chaos {ch['kind']}: {len(ch['events'])} fault events; app ready again {ch.get('recovery_seconds')} s after it ended"
        )
    print(f"  OVERSOLD SEATS: {db['oversold_seats']}   violated invariants: {db['violations'] or 'none'}")


if __name__ == "__main__":
    sys.exit(main())
