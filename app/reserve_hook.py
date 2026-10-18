"""Tells deye-reserve (DEYE_RESERVE_URL) that krem's schedule or grid changed, so it replans soon,
and asks it for today's sun forecast for the krem mini app.

Fire-and-forget: a slow or absent deye-reserve never holds up a collector
cycle or a grid message, and it has a poll of its own as a fallback.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class ReserveHook:
    TIMEOUT_S = 5.0
    # The forecast changes twice a day; the mini app refreshes far more often.
    SUN_FRESH_S = 300.0
    SUN_TIMEOUT_S = 2.0

    def __init__(self, url: Optional[str]):
        self.url = url.rstrip("/") if url else None
        self.sent = 0
        self.failures = 0
        self._tasks: set[asyncio.Task] = set()
        self._sun: Optional[dict] = None
        self._sun_until = 0.0

    async def sun(self) -> Optional[dict]:
        """Today's PV forecast from deye-reserve's /sun ({"p50_kwh", "low_kwh", ...}), cached; None if not there."""
        if not self.url:
            return None
        if time.monotonic() < self._sun_until:
            return self._sun
        sun = None
        try:
            timeout = aiohttp.ClientTimeout(total=self.SUN_TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.url}/sun") as r:
                    if r.status == 200:
                        sun = await r.json(content_type=None)
        except Exception as e:
            logger.debug(f"deye-reserve sun not reached: {type(e).__name__}: {e}")
        self._sun, self._sun_until = sun, time.monotonic() + self.SUN_FRESH_S
        return sun

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
