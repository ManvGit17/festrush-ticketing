"""Liveness and readiness probes."""

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

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
