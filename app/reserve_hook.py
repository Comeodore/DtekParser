"""Tells deye-reserve (DEYE_RESERVE_URL) that krem's schedule or grid changed, so it replans soon.

Fire-and-forget: a slow or absent deye-reserve never holds up a collector
cycle or a grid message, and it has a poll of its own as a fallback.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class ReserveHook:
    TIMEOUT_S = 5.0

    def __init__(self, url: Optional[str]):
        self.url = url.rstrip("/") if url else None
        self.sent = 0
        self.failures = 0
        self._tasks: set[asyncio.Task] = set()

    def fire(self, event: str, detail: str = "") -> None:
        if not self.url:
            return
        task = asyncio.get_running_loop().create_task(self._post(event, detail))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, event: str, detail: str) -> None:
        try:
            timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self.url}/events/{event}", json={"detail": detail}) as r:
                    if r.status >= 400:
                        raise RuntimeError(f"HTTP {r.status}")
            self.sent += 1
            logger.info(f"deye-reserve told: {event} {detail}".rstrip())
        except Exception as e:
            self.failures += 1
            logger.warning(f"deye-reserve not reached ({event}): {type(e).__name__}: {e}")
