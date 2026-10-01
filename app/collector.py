"""One loop per address: fetch, detect, persist and queue messages atomically."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional, Protocol

from app.config import SourceConfig
from app.dtek.client import FetchError, RawData
from app.dtek.legacy import legacy_row
from app.dtek.models import Snapshot
from app.dtek.protocol import ProtocolError, build_snapshot, parse_fact, parse_house, unknown_time_types
from app.engine.detector import Baseline, detect
from app.notify.router import Router
from app.storage.store import Store
from app.timeutil import KYIV_TZ, now_kyiv

logger = logging.getLogger(__name__)

# Three missed cycles: one slow or failed fetch is retried within seconds and is no reason for alarm.
STALE_AFTER = timedelta(minutes=3)


class Site(Protocol):
    async def fetch(self) -> RawData: ...


class SourceCollector:
    def __init__(
        self,
        config: SourceConfig,
        site: Site,
        store: Store,
        router: Router,
        interval: float = 60.0,
        clock: Callable[[], datetime] = now_kyiv,
        on_messages: Optional[Callable[[], None]] = None,
    ):
        self.config = config
        self._site = site
        self._store = store
        self._router = router
        self._interval = interval
        self._clock = clock
        self._on_messages = on_messages
        self.latest: Optional[Snapshot] = None
        self.last_success: Optional[datetime] = None
        self.failures = 0
        self.last_error = ""
        self._warned_time_types: set[str] = set()

    @property
    def name(self) -> str:
        return self.config.name

    def healthy(self, now: Optional[datetime] = None) -> bool:
        now = now or self._clock()
        return self.last_success is not None and now - self.last_success < STALE_AFTER

    async def restore(self) -> None:
        """Serve the last known snapshot until the first fetch succeeds."""
        state = await self._store.load_source_state(self.name)
        if state and state.snapshot:
            try:
                self.latest = Snapshot.from_dict(state.snapshot)
                self.last_success = state.last_success_at
            except (KeyError, ValueError) as e:
                logger.warning(f"[{self.name}] stored snapshot unreadable, ignoring: {e}")

    async def run(self) -> None:
        while True:
            ok = await self.cycle()
            # Retry a failed fetch sooner, backing off to the normal interval.
            delay = self._interval if ok else min(self._interval, 15.0 * 2 ** (self.failures - 1))
            await asyncio.sleep(delay)

    async def cycle(self) -> bool:
        now = self._clock()
        try:
            raw = await self._site.fetch()
        except FetchError as e:
            self._failed("fetch", e)
            return False
        try:
            snapshot = build_snapshot(
                self.name, parse_house(raw.answer, self.config.building), parse_fact(raw.fact), raw.names, now
            )
        except ProtocolError as e:
            self._failed("protocol", e)
            return False
        except Exception as e:
            self._failed(f"parse ({type(e).__name__})", e)
            return False
        self._warn_time_types(raw.time_types)

        try:
            async with self._store.transaction() as tx:
                state = await tx.load_source_state(self.name)
                baseline = Baseline.from_dict(state.baseline) if state else None
                detection = detect(baseline, snapshot, now, self.config.fingerprint)
                messages = self._router.for_events(self.config, detection.events, snapshot, now)
                await tx.save_source_state(self.name, snapshot.to_dict(), detection.baseline.to_dict(), now)
                await tx.save_legacy_schedule(self.config.schedule_id, legacy_row(snapshot, now.astimezone(KYIV_TZ).date()))
                await tx.enqueue(messages)
        except Exception as e:
            self._failed("store", e)
            return False

        if self.failures:
            logger.info(f"[{self.name}] recovered after {self.failures} failed cycles")
        self.failures, self.last_error = 0, ""
        self.latest, self.last_success = snapshot, now
        if detection.initialized:
            logger.info(f"[{self.name}] baseline initialised, no notifications for the current state")
        for event in detection.events:
            logger.info(f"[{self.name}] event: {event}")
        if messages and self._on_messages:
            self._on_messages()
        return True

    def _failed(self, stage: str, error: Exception) -> None:
        self.failures += 1
        text = f"{stage}: {error}"
        # Log the first failure, every change of error, and then every 10th repeat.
        if self.failures == 1 or text != self.last_error or self.failures % 10 == 0:
            log = logger.warning if self.failures < 3 else logger.error
            log(f"[{self.name}] cycle failed ({self.failures} in a row): {text}")
        self.last_error = text

    def _warn_time_types(self, time_types: list) -> None:
        for value in unknown_time_types(time_types):
            if value not in self._warned_time_types:
                self._warned_time_types.add(value)
                logger.warning(f"[{self.name}] DTEK declares an unknown slot value {value!r}")
