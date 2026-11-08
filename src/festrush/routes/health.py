"""Liveness and readiness probes."""

from fastapi import APIRouter, Request
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.responses import JSONResponse, Response

from festrush import metrics
from festrush.deps import connection

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readyz(request: Request) -> JSONResponse:
    try:
        with connection(request) as conn:
            conn.execute("SELECT 1")
    except Exception:
        return JSONResponse({"status": "database unavailable"}, status_code=503)
    return JSONResponse({"status": "ready"})


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics(request: Request) -> Response:
    """Prometheus scrape target. The gateway does not expose it; Prometheus reaches the app directly."""
    with connection(request) as conn:
        body = metrics.render(conn)
    return Response(body, media_type=CONTENT_TYPE_LATEST)
