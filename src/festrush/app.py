"""Application factory."""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, RedirectResponse, Response

from festrush import __version__, metrics, tracing
from festrush.config import Settings, get_settings
from festrush.db import make_pool
from festrush.deps import AppState, LoginRequired, current_user, render
from festrush.payments_service import make_provider
from festrush.routes import api, auth, events, health
from festrush.security import Csrf, Passwords

log = logging.getLogger("festrush")

PACKAGE_DIR = Path(__file__).resolve().parent

# No inline script or style anywhere, so the policy can be strict (audit S8).
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

ERROR_TITLES = {
    403: (
        "Request not allowed",
        "Your form expired or was sent from another site. Go back, reload the page and try again.",
    ),
    404: ("Not found", "We couldn't find that page."),
    405: ("Not allowed", "That page doesn't accept this kind of request."),
    422: ("That request wasn't valid", "Something in the request was missing or malformed."),
    429: ("Slow down", "Too many requests. Wait a moment and try again."),
}


def _secure(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        anyio.to_thread.current_default_thread_limiter().total_tokens = settings.threadpool_size
        pool = make_pool(settings.database_url, settings.pool_min_size, settings.pool_max_size)
        app.state.festrush = AppState(
            settings=settings,
            pool=pool,
            passwords=Passwords(insecure_fast=settings.insecure_fast_hashing),
            csrf=Csrf(settings.secret_key.get_secret_value()),
            templates=templates,
            provider=make_provider(pool, settings),
        )
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(
        title="FestRush",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"
        if path not in ("/metrics", "/healthz", "/readyz"):
            metrics.REQUEST_SECONDS.labels(path, request.method, str(response.status_code)).observe(
                time.perf_counter() - started
            )
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=3600"
            response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            return response
        return _secure(response)

    tracing.setup(settings, "festrush-web", app)

    # --- Errors: never show exception text to users (audit S5) -------------------------------

    def error_page(request: Request, status: int, title: str, message: str) -> Response:
        user = None
        try:
            user = current_user(request)
        except Exception:
            log.exception("could not resolve the user while rendering an error page")
        return _secure(
            render(
                request,
                "error.html",
                user,
                status_code=status,
                status=status,
                title=title,
                message=message,
                request_id=getattr(request.state, "request_id", ""),
            )
        )

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            return _secure(JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code))
        title, message = ERROR_TITLES.get(exc.status_code, ("Something went wrong", ""))
        return error_page(request, exc.status_code, title, message)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        if request.url.path.startswith("/api/"):
            return _secure(JSONResponse({"error": "invalid_request"}, status_code=422))
        title, message = ERROR_TITLES[422]
        return error_page(request, 422, title, message)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> Response:
        log.error(
            "unhandled error request_id=%s path=%s",
            getattr(request.state, "request_id", "-"),
            request.url.path,
            exc_info=exc,
        )
        if request.url.path.startswith("/api/"):
            return _secure(JSONResponse({"error": "internal_error"}, status_code=500))
        return error_page(
            request,
            500,
            "Something went wrong",
            "It's on us, not you. Quote the request ID below if you report it.",
        )

    # --- Routes ------------------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(events.router)
    app.include_router(api.router)

    if settings.debug:
        # The lab's deliberate-crash demo, kept for the course exercise but off by default
        # (audit A7). It needs FESTRUSH_DEBUG=true.
        @app.get("/_debug/crash", include_in_schema=False)
        def crash() -> None:
            raise ZeroDivisionError("debug crash")

    return app
