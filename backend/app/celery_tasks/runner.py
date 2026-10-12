import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from app.infrastructure.database.sessions import async_engine

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run a task's coroutine on a fresh event loop, then drop the pool.

    Every Celery task gets a new loop from asyncio.run, but the engine's pool
    is module-level and outlives it. asyncpg connections are bound to the loop
    that opened them, so the next task in the same worker process would reuse
    a connection from a dead loop and fail with "another operation is in
    progress" — every task after a worker's first one did. Disposing the pool
    inside the task's own loop makes the next task open fresh connections.
    """

    async def _run() -> T:
        try:
            return await coro
        finally:
            await async_engine.dispose()

    return asyncio.run(_run())
