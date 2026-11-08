"""Shared plumbing for the routes: app state, the current user, CSRF checks and rendering."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from fastapi import Form, HTTPException, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from festrush.config import Settings
from festrush.db import Conn, Pool
from festrush.mockpay import MockProvider
from festrush.security import CSRF_COOKIE, SESSION_COOKIE, Csrf, Passwords
from festrush.services import user_for_session


@dataclass
class AppState:
    settings: Settings
    pool: Pool
    passwords: Passwords
    csrf: Csrf
    templates: Jinja2Templates
    provider: MockProvider


class LoginRequired(Exception):
    """Raised by routes that need a signed-in user; the app answers with a redirect."""


def state(request: Request) -> AppState:
    st: AppState = request.app.state.festrush
    return st


def connection(request: Request) -> AbstractContextManager[Conn]:
    """A pooled connection that commits when the block ends normally."""
    return state(request).pool.connection()


def current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with connection(request) as conn:
        return user_for_session(conn, token)


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user is None:
        raise LoginRequired
    return user


def verify_csrf(request: Request, csrf_token: str = Form("")) -> None:
    if not state(request).csrf.valid(request.cookies.get(CSRF_COOKIE), csrf_token):
        raise HTTPException(status_code=403, detail="csrf")


def client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def render(
    request: Request,
    name: str,
    user: dict[str, Any] | None,
    status_code: int = 200,
    **context: Any,
) -> Response:
    """Render a page. Every page can contain forms, so every page gets a CSRF token."""
    st = state(request)
    cookie_value = request.cookies.get(CSRF_COOKIE)
    fresh = not cookie_value or len(cookie_value) > 64
    if fresh or cookie_value is None:
        cookie_value = st.csrf.new_cookie_value()
    response = st.templates.TemplateResponse(
        request,
        name,
        {"user": user, "csrf_token": st.csrf.form_token(cookie_value), **context},
        status_code=status_code,
    )
    if fresh:
        response.set_cookie(
            CSRF_COOKIE,
            cookie_value,
            httponly=True,
            samesite="lax",
            secure=st.settings.cookie_secure,
            path="/",
        )
    return response
