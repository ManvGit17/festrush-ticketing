"""Command line: ``festrush migrate | seed | serve | worker | check | rebuild-inventory``."""

import argparse
import logging
import os
import sys

from festrush import invariants, services, tracing, worker
from festrush.config import Settings, get_settings
from festrush.db import connect, make_pool, migrate
from festrush.payments_service import make_provider
from festrush.security import Passwords

DEMO_USER_COUNT = 5


def cmd_migrate(settings: Settings) -> None:
    applied = migrate(settings.database_url, settings.migrations_dir)
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none, already up to date'}")


def cmd_seed(settings: Settings, demo_users: int) -> None:
    """Load the demo events and, optionally, demo accounts. Safe to run repeatedly."""
    with connect(settings.database_url) as conn:
        events = services.seed_events(conn)
        created = 0
        if demo_users:
            password = os.environ.get("FESTRUSH_DEMO_PASSWORD", "festrush-demo")
            passwords = Passwords(insecure_fast=settings.insecure_fast_hashing)
            for i in range(1, demo_users + 1):
                name = f"demo{i:02d}"
                if services.find_user(conn, name) is None:
                    services.create_user(conn, name, passwords.hash(password))
                    created += 1
        conn.commit()
    print(f"seeded {events} events and {created} new demo account(s)")


def cmd_worker(settings: Settings) -> None:
    """Run the background worker until interrupted. Safe to kill and restart at any time."""
    pool = make_pool(settings.database_url, 1, 4)
    tracing.setup(settings, "festrush-worker")
    from prometheus_client import start_http_server

    start_http_server(settings.worker_metrics_port)
    print(f"worker started, metrics on :{settings.worker_metrics_port} (Ctrl-C to stop)")
    try:
        worker.run_forever(pool, settings, make_provider(pool, settings))
    except KeyboardInterrupt:
        pass
    finally:
        pool.close()


def cmd_payments(settings: Settings, host: str, port: int) -> None:
    """Run the mock payment provider as its own service."""
    import uvicorn

    from festrush.payments_service import create_payments_app

    uvicorn.run(create_payments_app(settings), host=host, port=port, log_level="warning")


def cmd_check(settings: Settings) -> int:
    """Run the invariants. Prints each check and returns 1 if any is violated."""
    with connect(settings.database_url) as conn:
        results = invariants.run_checks(conn)
    failed = 0
    for check in invariants.CHECKS:
        rows = results[check.name]
        mark = "ok  " if not rows else "FAIL"
        print(f"{mark} {check.name}: {check.description}")
        if rows:
            failed += 1
            for row in rows[:5]:
                print(f"       {row}")
    print(f"{len(invariants.CHECKS) - failed} of {len(invariants.CHECKS)} invariants hold")
    return 1 if failed else 0


def cmd_rebuild_inventory(settings: Settings) -> None:
    """Rebuild seats and counters from the reservations (after changing strategy or capacity)."""
    with connect(settings.database_url) as conn:
        conn.execute("SELECT rebuild_inventory()")
        conn.commit()
    print("inventory rebuilt")


def cmd_serve(settings: Settings, host: str, port: int, workers: int) -> None:
    import shutil
    import tempfile

    import uvicorn

    # Every web process writes its metrics here and /metrics merges them.
    metrics_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR") or tempfile.mkdtemp(prefix="festrush-metrics-")
    shutil.rmtree(metrics_dir, ignore_errors=True)
    os.makedirs(metrics_dir, exist_ok=True)
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = metrics_dir

    uvicorn.run(
        "festrush.app:create_app",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="festrush", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending database migrations")
    seed = sub.add_parser("seed", help="load demo events (and demo accounts)")
    seed.add_argument(
        "--demo-users",
        type=int,
        default=DEMO_USER_COUNT,
        help=f"accounts demo01..demoNN (default {DEMO_USER_COUNT}; 0 for none)",
    )
    serve = sub.add_parser("serve", help="run the web server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--workers", type=int, default=1, help="server processes")
    sub.add_parser("worker", help="run the background worker (holds, outbox, payments)")
    payments = sub.add_parser("payments", help="run the mock payment provider as a service")
    payments.add_argument("--host", default="127.0.0.1")
    payments.add_argument("--port", type=int, default=0, help="default: FESTRUSH_PAYMENTS_PORT (8070)")
    sub.add_parser("check", help="check the invariants; exit 1 if any fails")
    sub.add_parser("rebuild-inventory", help="rebuild seats and counters from reservations")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    if args.command == "migrate":
        cmd_migrate(settings)
    elif args.command == "seed":
        cmd_seed(settings, args.demo_users)
    elif args.command == "worker":
        cmd_worker(settings)
    elif args.command == "payments":
        cmd_payments(settings, args.host, args.port or settings.payments_port)
    elif args.command == "check":
        sys.exit(cmd_check(settings))
    elif args.command == "rebuild-inventory":
        cmd_rebuild_inventory(settings)
    else:
        cmd_serve(settings, args.host, args.port, args.workers)


if __name__ == "__main__":
    main()
