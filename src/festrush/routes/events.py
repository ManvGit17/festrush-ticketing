"""Browsing events, registering, and the signed-in user's own pages."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import RedirectResponse, Response

from festrush import services
from festrush.deps import connection, current_user, render, require_user, verify_csrf

router = APIRouter()

# Notices are looked up by code, never echoed from the query string.
NOTICES: dict[str, tuple[str, str]] = {
    "registered": ("ok", "You're in. See you there."),
    "already": ("info", "You were already registered for that event."),
    "full": ("error", "Sorry, that event just sold out."),
}


def _notice(code: str) -> tuple[str, str] | None:
    return NOTICES.get(code)


@router.get("/")
def home() -> Response:
    return RedirectResponse("/events", status_code=303)


@router.get("/events")
def events(request: Request, notice: str = "") -> Response:
    user = current_user(request)
    with connection(request) as conn:
        rows = services.list_events(conn)
        mine = services.registered_event_ids(conn, user["id"]) if user else set()
    return render(
        request, "events.html", user, events=rows, registered=mine, notice=_notice(notice)
    )


@router.post("/register_event/{event_id}", dependencies=[Depends(verify_csrf)])
def register_event(request: Request, event_id: int) -> Response:
    user: dict[str, Any] = require_user(request)
    with connection(request) as conn:
        outcome = services.register_for_event(conn, user["id"], event_id)
    if outcome is services.Outcome.NO_EVENT:
        raise HTTPException(status_code=404, detail="no such event")
    destination = "/events" if outcome is services.Outcome.FULL else "/my-events"
    return RedirectResponse(f"{destination}?notice={outcome.value}", status_code=303)


@router.get("/my-events")
def my_events(request: Request, notice: str = "") -> Response:
    user = require_user(request)
    with connection(request) as conn:
        rows = services.registrations_for(conn, user["id"])
    return render(request, "my_events.html", user, events=rows, notice=_notice(notice))


@router.get("/checkout")
def checkout(request: Request) -> Response:
    user = require_user(request)
    with connection(request) as conn:
        rows = services.registrations_for(conn, user["id"])
        total = services.total_fee(conn, user["id"])
    return render(request, "checkout.html", user, events=rows, total=total)
