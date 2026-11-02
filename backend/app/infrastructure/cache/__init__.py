"""Rate limiting for unauthenticated endpoints (slowapi).

Counters live in Redis when RATE_LIMIT_STORAGE_URL points there, so they
survive reloads and are shared across API processes; `memory://` is per
process.
"""

from slowapi import Limiter
from starlette.requests import Request

from app.core import settings


def client_ip(request: Request) -> str:
    """The address the nearest proxy saw.

    Behind ngrok every request arrives from the Docker gateway, so the peer
    address would put all users in one bucket. The rightmost X-Forwarded-For
    entry is the one the proxy appended; entries to its left are whatever the
    client sent and can be forged.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class RateLimits:
    AUTH_STRICT = "5/minute"
    PASSWORD = "3/minute"


limiter = Limiter(
    key_func=client_ip,
    storage_uri=settings.RATE_LIMIT_STORAGE_URL,
    enabled=settings.RATE_LIMIT_ENABLED,
)

__all__ = ["limiter", "RateLimits", "client_ip"]
