"""The JSON API. Authenticate with ``Authorization: Bearer <token>``.

Tokens are the same server-side sessions the web pages use; get one from ``POST /api/login``.
Bearer tokens are not sent automatically by browsers, so these routes need no CSRF token.
Writes accept an ``Idempotency-Key`` header: send the same key to retry safely.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from festrush import inventory, orders, services
from festrush.deps import client_id, connection, state
from festrush.idempotency import MAX_KEY_LENGTH, Replayable, request_hash, run_idempotent
from festrush.inventory import Outcome
from festrush.security import SESSION_COOKIE  # noqa: F401 - documents the shared session store

router = APIRouter(prefix="/api", tags=["api"])


class Problem(BaseModel):
    error: str


class EventOut(BaseModel):
    id: int
    name: str
    fee: int
    capacity: int
    seats_left: int


class ReservationOut(BaseModel):
    id: int
    event_id: int
    status: str
    expires_at: str | None = None


class OrderItem(BaseModel):
    reservation_id: int
    event_id: int
    name: str
    fee: int
    status: str


class OrderOut(BaseModel):
    id: int
    status: str
    total: int
    payment_status: str | None = None
    items: list[OrderItem] = []


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class TokenOut(BaseModel):
    token: str


def api_user(request: Request, authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    with connection(request) as conn:
        user = services.user_for_session(conn, token.strip())
    if user is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return user


CurrentUser = Annotated[dict[str, Any], Depends(api_user)]
IdemKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", max_length=MAX_KEY_LENGTH, description="Makes retries safe"),
]


def _respond(result: Replayable) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(result.body, status_code=result.status, headers=headers)


@router.post("/login", response_model=TokenOut, responses={401: {"model": Problem}})
def login(request: Request, body: LoginIn) -> dict[str, str]:
    st = state(request)
    client = client_id(request)
    with connection(request) as conn:
        blocked = services.login_blocked(
            conn,
            body.username,
            client,
            max_failures=st.settings.login_max_failures,
            minutes=st.settings.login_window_minutes,
        )
        user = None if blocked else services.find_user(conn, body.username)
    if blocked:
        raise HTTPException(status_code=429, detail="too many failed attempts")
    if not st.passwords.verify(user["password_hash"] if user else None, body.password) or user is None:
        with connection(request) as conn:
            services.record_login_failure(conn, body.username, client)
        raise HTTPException(status_code=401, detail="invalid username or password")
    with connection(request) as conn:
        token = services.create_session(conn, user["id"], st.settings.session_hours)
    return {"token": token}


@router.get("/events", response_model=list[EventOut])
def list_events(request: Request) -> list[dict[str, Any]]:
    with connection(request) as conn:
        return services.list_events(conn)


@router.get("/events/{event_id}", response_model=EventOut, responses={404: {"model": Problem}})
def get_event(request: Request, event_id: int) -> dict[str, Any]:
    with connection(request) as conn:
        found = services.get_event(conn, event_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such event")
    return found


@router.post(
    "/events/{event_id}/reserve",
    status_code=201,
    response_model=ReservationOut,
    responses={
        200: {"description": "A retry with the same Idempotency-Key: the first answer, replayed"},
        404: {"model": Problem},
        409: {"model": Problem, "description": "sold_out, already_reserved or busy"},
        422: {"model": Problem, "description": "Idempotency-Key reused for a different request"},
    },
)
def reserve(
    request: Request,
    event_id: int,
    user: CurrentUser,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    """Hold one seat for ten minutes (configurable). Pay for it with ``POST /api/orders``."""
    st = state(request)

    def work(conn):  # type: ignore[no-untyped-def]
        result = inventory.reserve(conn, st.settings, user["id"], event_id)
        if result.outcome is Outcome.HELD:
            return 201, {
                "id": result.reservation_id,
                "event_id": event_id,
                "status": "held",
                "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            }
        status, error = {
            Outcome.ALREADY: (409, "already_reserved"),
            Outcome.FULL: (409, "sold_out"),
            Outcome.BUSY: (409, "busy"),
            Outcome.NO_EVENT: (404, "no_such_event"),
        }[result.outcome]
        return status, {"error": error}

    result = run_idempotent(st.pool, user["id"], idempotency_key, request_hash("reserve", event_id), work)
    return _respond(result)


@router.post(
    "/orders",
    status_code=201,
    response_model=OrderOut,
    responses={409: {"model": Problem, "description": "no_holds"}, 422: {"model": Problem}},
)
def create_order(request: Request, user: CurrentUser, idempotency_key: IdemKey = None) -> JSONResponse:
    """Make an order from every seat you currently hold. Then pay with ``POST /api/orders/{id}/pay``."""
    st = state(request)

    def work(conn):  # type: ignore[no-untyped-def]
        ref = orders.create_order(conn, user["id"])
        if ref.id is None:
            return 409, {"error": "no_holds"}
        order = orders.get_order(conn, ref.id)
        assert order is not None
        return 201, OrderOut.model_validate(order).model_dump()

    result = run_idempotent(st.pool, user["id"], idempotency_key, request_hash("order"), work)
    return _respond(result)


@router.post("/orders/{order_id}/pay", response_model=OrderOut, responses={404: {"model": Problem}})
def pay_order(request: Request, order_id: int, user: CurrentUser) -> dict[str, Any]:
    """Pay for an order. Safe to repeat: only one call ever reaches the payment provider."""
    st = state(request)
    with connection(request) as conn:
        if orders.get_order(conn, order_id, user["id"]) is None:
            raise HTTPException(status_code=404, detail="no such order")
    orders.run_payment(st.pool, st.provider, order_id)
    with connection(request) as conn:
        order = orders.get_order(conn, order_id, user["id"])
    assert order is not None
    return order


@router.get("/orders/{order_id}", response_model=OrderOut, responses={404: {"model": Problem}})
def read_order(request: Request, order_id: int, user: CurrentUser) -> dict[str, Any]:
    with connection(request) as conn:
        order = orders.get_order(conn, order_id, user["id"])
    if order is None:
        raise HTTPException(status_code=404, detail="no such order")
    return order


@router.get("/me/reservations", response_model=list[ReservationOut])
def my_reservations(request: Request, user: CurrentUser) -> list[dict[str, Any]]:
    with connection(request) as conn:
        rows = services.my_reservations(conn, user["id"])
    return [
        {
            "id": r["id"],
            "event_id": r["event_id"],
            "status": r["status"],
            "expires_at": r["expires_at"].isoformat(),
        }
        for r in rows
    ]
