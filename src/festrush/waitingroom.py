"""A virtual waiting room: serve fewer people well instead of everyone badly.

When thousands of students arrive in the same second, no strategy for claiming a seat makes
5,000 simultaneous requests fast (docs/concurrency.md). The waiting room admits students at a
rate the system can serve. Students join the queue (cheap), poll their position (cheap), and may
reserve only while admitted.

Two admission orders, chosen by ``waiting_room_mode``:

* ``fifo``     first come, first admitted. Simple and easy to explain, but it rewards the fastest
               connection and any script that joins in the first milliseconds.
* ``lottery``  each tick admits a random sample of everyone waiting, so arriving a millisecond
               earlier buys nothing. Fairer against bots; positions are meaningless, so the
               status reports how many are waiting instead.
"""

from typing import Any

from festrush.config import Settings
from festrush.db import Conn, one

_ADMIT = (
    "UPDATE queue_entries SET admitted_at = now(), admitted_until = now() + make_interval(mins => %s)"
    " WHERE user_id IN (SELECT user_id FROM queue_entries WHERE admitted_at IS NULL"
    " ORDER BY {order} LIMIT %s FOR UPDATE SKIP LOCKED)"
)
_ADMIT_FIFO = _ADMIT.replace("{order}", "seq")
_ADMIT_LOTTERY = _ADMIT.replace("{order}", "random()")


def join(conn: Conn, user_id: int) -> None:
    """Join the queue. Joining twice keeps the original place."""
    conn.execute("INSERT INTO queue_entries (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))


def status(conn: Conn, settings: Settings, user_id: int) -> dict[str, Any]:
    me = one(
        conn,
        "SELECT seq, admitted_at IS NOT NULL AS admitted, admitted_until, admitted_until > now() AS active"
        " FROM queue_entries WHERE user_id = %s",
        (user_id,),
    )
    if me is None:
        return {"joined": False, "admitted": False}
    waiting = one(
        conn,
        "SELECT count(*) AS n, count(*) FILTER (WHERE seq < %s) AS ahead FROM queue_entries"
        " WHERE admitted_at IS NULL",
        (me["seq"],),
    )
    assert waiting is not None
    rate = max(settings.waiting_room_admit_per_second, 1)
    body: dict[str, Any] = {
        "joined": True,
        "admitted": bool(me["active"]),
        "mode": settings.waiting_room_mode,
        "waiting": int(waiting["n"]),
    }
    if me["admitted"]:
        body["admitted_until"] = me["admitted_until"].isoformat()
        body["expired"] = not me["active"]
    elif settings.waiting_room_mode == "fifo":
        body["position"] = int(waiting["ahead"]) + 1
        body["estimated_wait_seconds"] = round((int(waiting["ahead"]) + 1) / rate, 1)
    else:
        body["estimated_wait_seconds"] = round(int(waiting["n"]) / rate / 2, 1)  # about half the queue
    if "estimated_wait_seconds" in body:
        # Tell the client when to ask again, so a long queue does not turn into a polling storm.
        body["poll_after_seconds"] = round(min(30.0, max(2.0, body["estimated_wait_seconds"] / 5)), 1)
    return body


def is_admitted(conn: Conn, user_id: int) -> bool:
    row = one(
        conn, "SELECT 1 AS x FROM queue_entries WHERE user_id = %s AND admitted_until > now()", (user_id,)
    )
    return row is not None


def admit_next(conn: Conn, settings: Settings, count: int) -> int:
    """Admit up to ``count`` waiting students. Returns how many were admitted."""
    if count <= 0:
        return 0
    query = _ADMIT_FIFO if settings.waiting_room_mode == "fifo" else _ADMIT_LOTTERY
    cur = conn.execute(query, (settings.waiting_room_admit_minutes, count))
    return cur.rowcount


def purge_expired(conn: Conn) -> int:
    """Forget students whose admission ran out long ago (they can join again)."""
    cur = conn.execute("DELETE FROM queue_entries WHERE admitted_until < now() - interval '1 hour'")
    return cur.rowcount
