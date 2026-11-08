"""Browsing events, holding seats, and paying: the server-rendered pages."""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse, Response

from festrush import inventory, orders, services
from festrush.deps import connection, current_user, render, require_user, state, verify_csrf
from festrush.idempotency import request_hash, run_idempotent
from festrush.inventory import Outcome

router = APIRouter()


def _notice(request: Request, code: str) -> tuple[str, str] | None:
    """Notices are looked up by code, never echoed from the query string."""
    minutes = state(request).settings.hold_minutes
    known: dict[str, tuple[str, str]] = {
        "held": ("ok", f"Seat held for {minutes} minutes. Check out to confirm it."),
        "already": ("info", "You already hold or own a seat at that event."),
        "full": ("error", "Sorry, that event just sold out."),
        "busy": ("error", "A lot of people are booking right now. Try again in a moment."),
        "nothing": ("info", "You have no seats on hold to check out."),
    }
    return known.get(code)


@router.get("/")
def home() -> Response:
    return RedirectResponse("/events", status_code=303)


@router.get("/events")
def events(request: Request, notice: str = "") -> Response:
    user = current_user(request)
    with connection(request) as conn:
        rows = services.list_events(conn)
        mine = (
            {r["event_id"]: r["status"] for r in services.my_reservations(conn, user["id"])} if user else {}
        )
    return render(request, "events.html", user, events=rows, mine=mine, notice=_notice(request, notice))


@router.post("/register_event/{event_id}", dependencies=[Depends(verify_csrf)])
def register_event(request: Request, event_id: int) -> Response:
    """Hold a seat. (The name is kept from v1; what it does now is reserve, not register.)"""
    user = require_user(request)
    st = state(request)
    with connection(request) as conn:
        result = inventory.reserve(conn, st.settings, user["id"], event_id)
    if result.outcome is Outcome.NO_EVENT:
        raise HTTPException(status_code=404, detail="no such event")
    destination = "/my-events" if result.outcome in (Outcome.HELD, Outcome.ALREADY) else "/events"
    return RedirectResponse(f"{destination}?notice={result.outcome.value}", status_code=303)


@router.get("/my-events")
def my_events(request: Request, notice: str = "") -> Response:
    user = require_user(request)
    with connection(request) as conn:
        rows = services.my_reservations(conn, user["id"])
    return render(request, "my_events.html", user, events=rows, notice=_notice(request, notice))


@router.get("/checkout")
def checkout(request: Request) -> Response:
    user = require_user(request)
    with connection(request) as conn:
        rows = services.my_reservations(conn, user["id"])
        past = services.my_orders(conn, user["id"])
    holds = [r for r in rows if r["status"] == "held" and r["order_id"] is None]
    return render(
        request,
        "checkout.html",
        user,
        holds=holds,
        total=sum(int(h["fee"]) for h in holds),
        orders=past,
        idempotency_key=uuid.uuid4().hex,
    )


@router.post("/checkout", dependencies=[Depends(verify_csrf)])
def pay(request: Request, idempotency_key: str = Form("")) -> Response:
    """Create the order and pay for it. A double click or a retry carries the same key from the
    form, so it gets the same order back instead of making a second one."""
    user = require_user(request)
    st = state(request)
    key = idempotency_key.strip()[:200] or uuid.uuid4().hex

    def work(conn):  # type: ignore[no-untyped-def]
        ref = orders.create_order(conn, user["id"])
        if ref.id is None:
            return 409, {"error": "no_holds"}
        return 201, {"order_id": ref.id}

    result = run_idempotent(st.pool, user["id"], key, request_hash("checkout"), work)
    order_id = result.body.get("order_id")
    if not order_id:
        return RedirectResponse("/my-events?notice=nothing", status_code=303)
    orders.run_payment(st.pool, st.provider, int(order_id))
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.get("/orders/{order_id}")
def order_page(request: Request, order_id: int) -> Response:
    user = require_user(request)
    with connection(request) as conn:
        order = orders.get_order(conn, order_id, user["id"])
        messages = conn.execute(
            "SELECT kind, subject, body FROM mailbox WHERE order_id = %s ORDER BY id", (order_id,)
        ).fetchall()
    if order is None:
        raise HTTPException(status_code=404, detail="no such order")
    return render(request, "order.html", user, order=order, messages=messages)
