"""Command line: ``festrush migrate | seed | serve``."""

import argparse
import logging
import os

from festrush import services
from festrush.config import Settings, get_settings
from festrush.db import connect, migrate
from festrush.security import Passwords

DEMO_USER_COUNT = 5


def cmd_migrate(settings: Settings) -> None:
    applied = migrate(settings.database_url, settings.migrations_dir)
    print(
        f"applied {len(applied)} migration(s): {', '.join(applied) or 'none, already up to date'}"
    )


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


def cmd_serve(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(
        "festrush.app:create_app",
        factory=True,
        host=host,
        port=port,
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    if args.command == "migrate":
        cmd_migrate(settings)
    elif args.command == "seed":
        cmd_seed(settings, args.demo_users)
    else:
        cmd_serve(settings, args.host, args.port)


if __name__ == "__main__":
    main()
