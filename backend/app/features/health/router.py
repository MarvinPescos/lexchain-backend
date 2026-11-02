"""Service availability, for uptime checks and the Docker healthcheck.

`/health` answers as long as the process serves requests. `/health/ready`
also checks what a request depends on: Postgres, Redis, and at least one
Celery worker (without one, finalized documents queue but never process).
Failures are logged with the reason; the response only says which part is down.
"""

import asyncio

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from redis.asyncio import from_url as redis_from_url
from sqlalchemy import text

from app.core import settings
from app.infrastructure.database import AsyncSessionLocal
from app.infrastructure.observability.logging_setup import log

router = APIRouter(prefix="/health", tags=["Health"])

CHECK_TIMEOUT = 3.0


async def _database() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))


async def _redis() -> None:
    client = redis_from_url(settings.CELERY_BROKER_URL)
    try:
        await client.ping()
    finally:
        await client.close()


async def _workers() -> None:
    from app.celery_app import celery_app

    replies = await asyncio.to_thread(celery_app.control.ping, timeout=1.0)
    if not replies:
        raise RuntimeError("no Celery worker answered")


CHECKS = {"database": _database, "redis": _redis, "workers": _workers}


async def _run(name: str) -> str:
    try:
        await asyncio.wait_for(CHECKS[name](), timeout=CHECK_TIMEOUT)
        return "ok"
    except Exception as e:
        log.error("health.check.failed", check=name, error=str(e) or type(e).__name__)
        return "down"


@router.get("", summary="Liveness", responses={200: {"description": "Process is up"}})
async def health() -> dict:
    return {"status": "ok"}


@router.get(
    "/ready",
    summary="Readiness",
    responses={
        200: {"description": "Every dependency is reachable"},
        503: {"description": "At least one dependency is down"},
    },
)
async def ready() -> JSONResponse:
    results = await asyncio.gather(*(_run(name) for name in CHECKS))
    checks = dict(zip(CHECKS, results))
    healthy = all(r == "ok" for r in results)
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
