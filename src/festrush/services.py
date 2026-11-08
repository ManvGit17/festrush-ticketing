"""Database operations. Every function takes a connection and leaves committing to the caller,
except where a function documents that it runs its own transaction."""

import enum
import re
from datetime import timedelta
from typing import Any

import psycopg.errors

from festrush.db import Conn, one
from festrush.security import hash_token, new_session_token

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
PASSWORD_MIN, PASSWORD_MAX = 8, 128


def validate_credentials(username: str, password: str) -> str | None:
    """Return a user-facing problem with the credentials, or None if they are acceptable."""
    if not USERNAME_RE.fullmatch(username):
        return "Usernames are 3 to 32 characters: letters, digits, dot, dash or underscore."
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return f"Passwords are {PASSWORD_MIN} to {PASSWORD_MAX} characters."
    return None


# --- Users and sessions --------------------------------------------------------------------


def create_user(conn: Conn, username: str, password_hash: str) -> int | None:
    """Insert a user. Returns the new id, or None if the name (any case) is taken."""
    row = one(
        conn,
        "INSERT INTO users (username, password_hash) VALUES (%s, %s)"
        " ON CONFLICT DO NOTHING RETURNING id",
        (username, password_hash),
    )
    return None if row is None else int(row["id"])


def find_user(conn: Conn, username: str) -> dict[str, Any] | None:
    return one(
        conn,
        "SELECT id, username, password_hash FROM users WHERE lower(username) = lower(%s)",
        (username,),
    )


def set_password_hash(conn: Conn, user_id: int, password_hash: str) -> None:
    conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))


def create_session(conn: Conn, user_id: int, hours: int) -> str:
    token = new_session_token()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, now() + %s)",
        (hash_token(token), user_id, timedelta(hours=hours)),
    )
    # Expired sessions are cleared as new ones are made, so no separate cleanup job is needed.
    conn.execute("DELETE FROM sessions WHERE expires_at < now()")
    return token


def user_for_session(conn: Conn, token: str) -> dict[str, Any] | None:
    return one(
        conn,
        "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = %s AND s.expires_at > now()",
        (hash_token(token),),
    )


def delete_session(conn: Conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_token(token),))


# --- Login throttling ----------------------------------------------------------------------


def login_blocked(
    conn: Conn, username: str, client: str, *, max_failures: int, minutes: int
) -> bool:
    row = one(
        conn,
        "SELECT count(*) AS n FROM login_failures"
        " WHERE username = lower(%s) AND client = %s AND attempted_at > now() - %s",
        (username, client, timedelta(minutes=minutes)),
    )
    return row is not None and int(row["n"]) >= max_failures


def record_login_failure(conn: Conn, username: str, client: str) -> None:
    conn.execute(
        "INSERT INTO login_failures (username, client) VALUES (lower(%s), %s)", (username, client)
    )
    conn.execute("DELETE FROM login_failures WHERE attempted_at < now() - interval '1 day'")


def clear_login_failures(conn: Conn, username: str, client: str) -> None:
    conn.execute(
        "DELETE FROM login_failures WHERE username = lower(%s) AND client = %s", (username, client)
    )


# --- Events and registrations --------------------------------------------------------------


def list_events(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id, name, description, fee, capacity, seats_taken,"
        " capacity - seats_taken AS seats_left FROM events ORDER BY id"
    ).fetchall()


def registered_event_ids(conn: Conn, user_id: int) -> set[int]:
    rows = conn.execute("SELECT event_id FROM registrations WHERE user_id = %s", (user_id,))
    return {int(r["event_id"]) for r in rows}


def registrations_for(conn: Conn, user_id: int) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT e.id, e.name, e.fee, r.created_at FROM registrations r"
        " JOIN events e ON e.id = r.event_id WHERE r.user_id = %s ORDER BY r.created_at, r.id",
        (user_id,),
    ).fetchall()


def total_fee(conn: Conn, user_id: int) -> int:
    """What this user owes: the fees of the events they registered for, and nobody else's."""
    row = one(
        conn,
        "SELECT COALESCE(sum(e.fee), 0)::int AS total FROM registrations r"
        " JOIN events e ON e.id = r.event_id WHERE r.user_id = %s",
        (user_id,),
    )
    return int(row["total"]) if row else 0


class Outcome(enum.Enum):
    REGISTERED = "registered"
    ALREADY = "already"
    FULL = "full"
    NO_EVENT = "no_event"


class _Rollback(Exception):
    pass


def register_for_event(conn: Conn, user_id: int, event_id: int) -> Outcome:
    """Register a user for an event; safe against double clicks and concurrent buyers.

    Runs in its own transaction. Two requests for the same (user, event) collide on the unique
    constraint, so only one row is created. The seat is claimed with a single conditional
    UPDATE, so the counter can never pass the capacity however many requests race.
    """
    if one(conn, "SELECT 1 AS x FROM events WHERE id = %s", (event_id,)) is None:
        return Outcome.NO_EVENT
    try:
        with conn.transaction():
            created = one(
                conn,
                "INSERT INTO registrations (user_id, event_id) VALUES (%s, %s)"
                " ON CONFLICT (user_id, event_id) DO NOTHING RETURNING id",
                (user_id, event_id),
            )
            if created is None:
                return Outcome.ALREADY
            claimed = one(
                conn,
                "UPDATE events SET seats_taken = seats_taken + 1"
                " WHERE id = %s AND seats_taken < capacity RETURNING id",
                (event_id,),
            )
            if claimed is None:
                raise _Rollback  # undo the registration row
    except _Rollback:
        return Outcome.FULL
    except psycopg.errors.ForeignKeyViolation:
        return Outcome.NO_EVENT
    return Outcome.REGISTERED


# --- Seeding -------------------------------------------------------------------------------

SEED_EVENTS: list[tuple[str, str, int, int]] = [
    ("Hackathon", "24 hours, one problem statement, zero sleep.", 500, 150),
    ("Dance Battle", "Crews face off on the main stage.", 300, 80),
    ("AI Workshop", "Hands-on session on building small language tools.", 400, 60),
    ("Photography Walk", "Golden-hour walk across campus with a mentor.", 200, 40),
    ("Gaming Tournament", "Bracketed 5v5 on the gaming rigs.", 350, 128),
    ("Music Night", "Bands, acoustic sets and an open mic.", 250, 300),
    ("Treasure Hunt", "Teams of four, clues across the whole campus.", 150, 200),
    ("Stand-up Comedy", "An evening of student stand-up.", 300, 120),
    ("Robo Race", "Line-follower robots race for the cup.", 450, 50),
]


def seed_events(conn: Conn) -> int:
    """Insert or refresh the demo events. Safe to run any number of times."""
    for name, description, fee, capacity in SEED_EVENTS:
        conn.execute(
            "INSERT INTO events (name, description, fee, capacity) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description,"
            " fee = EXCLUDED.fee, capacity = GREATEST(events.seats_taken, EXCLUDED.capacity)",
            (name, description, fee, capacity),
        )
    return len(SEED_EVENTS)
