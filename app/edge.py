"""The reserve on Cloudflare (cf-watch/): it reads DTEK on its own and messages the chats while this service is away.

The two take turns and never message a source's chats at the same time. After each good cycle a source pushes
its baseline, bot and chats to the reserve; the pushes are also how the reserve knows the service is alive, and
it takes a source over only once they have stopped for its lease (5 minutes). A source that has not pushed for
AWAY_AFTER first claims the source back: the reserve hands over the baseline it went on with, so the difference
reported next starts from what the reserve already sent and nothing is sent twice. Only DTEK's news goes through
the reserve; power messages stay with this service.

A failed call leaves the service working as without the reserve: it would rather repeat a message than lose one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Mapping, Optional

import aiohttp

from app.config import SourceConfig

logger = logging.getLogger(__name__)

# Well inside the reserve's lease, so a source that is back claims before the reserve could have sent anything new.
AWAY_AFTER = timedelta(minutes=3)
TIMEOUT = aiohttp.ClientTimeout(total=5)


class Edge:
    def __init__(self, url: str, key: str, bots: Mapping[str, str]):
        self.url = url.rstrip("/")
        self._key = key
        self._bots = dict(bots)
        self._session: Optional[aiohttp.ClientSession] = None
        self._pushed: dict[str, datetime] = {}
        self._errors: dict[str, str] = {}

    def away(self, source: str, now: datetime) -> bool:
        """Whether the reserve may have taken this source over since its last push (or since the start)."""
        last = self._pushed.get(source)
        return last is None or now - last > AWAY_AFTER

    async def claim(self, source: str) -> Optional[dict]:
        """The baseline the reserve went on with while this source was away; None when it did not take over
        (or cannot be reached)."""
        answer = await self._post(source, "claim", {})
        baseline = answer.get("baseline") if answer else None
        if baseline:
            logger.warning(f"[{source}] back from the reserve, which queued {answer.get('queued') or 0} messages "
                           f"meanwhile; going on from its baseline")
        return baseline

    async def push(self, config: SourceConfig, baseline: dict, now: datetime) -> None:
        body = {
            "baseline": baseline,
            "fingerprint": config.fingerprint,
            "lang": config.lang,
            "notify_outages": config.notify_outages,
            "chat_ids": list(config.chat_ids),
            "token": self._bots.get(config.bot, ""),
        }
        if await self._post(config.name, "home", body) is not None:
            self._pushed[config.name] = now

    async def _post(self, source: str, action: str, body: dict) -> Optional[dict]:
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession(timeout=TIMEOUT)
            async with self._session.post(f"{self.url}/{action}/{source}", json=body,
                                          headers={"authorization": f"Bearer {self._key}"}) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}: {(await response.text())[:120]}")
                answer = await response.json()
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            # Once per kind of failure, not every cycle.
            if self._errors.get(source) != error:
                logger.warning(f"[{source}] reserve {action} failed: {error}")
            self._errors[source] = error
            return None
        if self._errors.pop(source, None) is not None:
            logger.info(f"[{source}] reserve reachable again")
        return answer

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
