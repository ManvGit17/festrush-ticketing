"""Live views: a seat map pushed over server-sent events, and the System Lens snapshot.

The seat map is counted from reservations (the source of truth for every claiming strategy), so
it shows how many seats are free, held and sold, and which of the held or sold ones are yours.
"""

import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response, StreamingResponse

from festrush import metrics, services
from festrush.db import Conn, one
from festrush.deps import connection, current_user, render, state

router = APIRouter()

STREAM_SECONDS = 55  # the browser reconnects by itself; this keeps connections from piling up


def seat_map(conn: Conn, event_id: int, user_id: int | None) -> dict[str, Any] | None:
    event = services.get_event(conn, event_id)
    if event is None:
        return None
    rows = conn.execute(
        "SELECT status, COALESCE(user_id = %s, false) AS mine, count(*)::int AS n FROM reservations"
        " WHERE event_id = %s AND status IN ('held', 'confirmed') GROUP BY 1, 2",
        (user_id, event_id),
    ).fetchall()
    counts = {"held": 0, "sold": 0, "mine_held": 0, "mine_sold": 0}
    for r in rows:
        key = "held" if r["status"] == "held" else "sold"
        counts[key] += r["n"]
        if r["mine"]:
            counts["mine_" + key] += r["n"]
    capacity = int(event["capacity"])
    cells = (
        ["mine_sold"] * counts["mine_sold"]
        + ["sold"] * (counts["sold"] - counts["mine_sold"])
        + ["mine_held"] * counts["mine_held"]
        + ["held"] * (counts["held"] - counts["mine_held"])
    )[:capacity]
    cells += ["free"] * (capacity - len(cells))
    return {
        "event_id": event_id,
        "name": event["name"],
        "capacity": capacity,
        "cells": cells,
        "free": cells.count("free"),
    }


def lens(conn: Conn) -> dict[str, float]:
    """The same numbers the Grafana dashboard draws, as JSON for the in-app drawer."""
    out: dict[str, float] = {}
    for name, (_help, query) in metrics._GAUGE_SQL.items():
        row = one(conn, query)
        out[name.removeprefix("festrush_")] = float(next(iter(row.values()))) if row else 0.0
    return out


@router.get("/events/{event_id}/live")
def live_page(request: Request, event_id: int) -> Response:
    user = current_user(request)
    with connection(request) as conn:
        snap = seat_map(conn, event_id, user["id"] if user else None)
    if snap is None:
        raise HTTPException(status_code=404, detail="no such event")
    return render(request, "live.html", user, snap=snap, event_id=event_id)


@router.get("/live/events/{event_id}/seats")
def seats_now(request: Request, event_id: int) -> Response:
    user = current_user(request)
    with connection(request) as conn:
        snap = seat_map(conn, event_id, user["id"] if user else None)
    if snap is None:
        raise HTTPException(status_code=404, detail="no such event")
    return Response(json.dumps(snap), media_type="application/json")


@router.get("/live/events/{event_id}/stream")
def seats_stream(request: Request, event_id: int, once: bool = False) -> Response:
    """Server-sent events: one message now, then one whenever the seat map changes."""
    user = current_user(request)
    uid = user["id"] if user else None
    pool = state(request).pool

    def events() -> Iterator[str]:
        last = None
        deadline = time.monotonic() + STREAM_SECONDS
        yield "retry: 2000\n\n"
        while time.monotonic() < deadline:
            with pool.connection() as conn:
                snap = seat_map(conn, event_id, uid)
            if snap is None:
                return
            if snap != last:
                last = snap
                yield f"data: {json.dumps(snap)}\n\n"
            if once:
                return
            time.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/live/lens")
def lens_now(request: Request) -> Response:
    """System Lens data. Signed-in users only: it describes the whole system."""
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="sign in")
    with connection(request) as conn:
        body = lens(conn)
    return Response(json.dumps(body), media_type="application/json")
