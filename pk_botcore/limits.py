"""Process-wide asynchronous resource limits."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from weakref import WeakKeyDictionary

logger = logging.getLogger("pk_botcore.limits")

_semaphores: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    WeakKeyDictionary()
)
_semaphore_limits: WeakKeyDictionary[asyncio.AbstractEventLoop, int] = (
    WeakKeyDictionary()
)


def _llm_concurrency_limit() -> int:
    raw = os.getenv("PK_BOTCORE_LLM_CONCURRENCY", "4")
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("Invalid PK_BOTCORE_LLM_CONCURRENCY=%r; using 4", raw)
        return 4


@asynccontextmanager
async def llm_slot():
    """Limit simultaneous LLM operations within the current event loop."""
    loop = asyncio.get_running_loop()
    limit = _llm_concurrency_limit()
    semaphore = _semaphores.get(loop)
    if semaphore is None or _semaphore_limits.get(loop) != limit:
        semaphore = asyncio.Semaphore(limit)
        _semaphores[loop] = semaphore
        _semaphore_limits[loop] = limit

    async with semaphore:
        yield
