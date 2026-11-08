"""One loop per address: fetch, detect, persist and queue messages atomically."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Callable, Optional, Protocol

from app.config import SourceConfig
from app.dtek.client import FetchError, RawData, SiteDown
from app.dtek.legacy import legacy_row
from app.dtek.models import Snapshot
from app.dtek.protocol import ProtocolError, build_snapshot, parse_fact, parse_house, unknown_time_types
from app.edge import Edge
from app.engine.detector import Baseline, detect
from app.engine.events import ScheduleEvent
from app.notify.router import Router
from app.storage.store import Store
from app.timeutil import KYIV_TZ, now_kyiv

logger = logging.getLogger(__name__)

# Three missed cycles: one slow or failed fetch is retried within seconds and is no reason for alarm.
STALE_AFTER = timedelta(minutes=3)
# DTEK's own site being down or overloaded (it is, for a while after most schedule changes) is waited out:
# nothing on our side would fix it. Only an outage this long is worth a look.
SITE_DOWN_PATIENCE = timedelta(hours=3)


def error_kind(error: str) -> str:
    """A few words for an error, the same each time it repeats: Incapsula's challenge, an HTTP status, a timeout,
    or else where it failed and the exception's name."""
    if "challenge" in error or "hCaptcha" in error:
        return "Incapsula challenge"
    if "no schedule data" in error:
        return "page not recognised"
    if "down or overloaded" in error:
        return "site overloaded"
    status = re.search(r"HTTP (\d{3})", error)
    if status:
        return f"HTTP {status.group(1)}"
    if any(word in error for word in ("Timeout", "timed out", "AbortError", "still loading", "still blank", "does not answer")):
        return "timeout"
    stage, _, rest = error.partition(": ")
    name = re.match(r"\w+", rest)
    return f"{stage}: {name.group(0)}" if name else stage


def _kind(error: str) -> str:
    return f" ({error_kind(error)})" if error else ""


def _moment(moment: Optional[datetime]) -> str:
    return moment.astimezone(KYIV_TZ).strftime("%d.%m %H:%M") if moment else "the start"


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
        on_schedule: Optional[Callable[[str], None]] = None,
        edge: Optional[Edge] = None,
    ):
        self.config = config
        self._edge = edge
        self._site = site
        self._store = store
        self._router = router
        self._interval = interval
        self._clock = clock
        self._on_messages = on_messages
        # Called with DTEK's new "last updated" stamp when the schedule data changes (not on the first fetch).
        self._on_schedule = on_schedule
        self.latest: Optional[Snapshot] = None
        self.last_success: Optional[datetime] = None
        self.failures = 0
        self.last_error = ""
        # The error that began the current run of failures; it names the trouble in the alert while it lasts.
        self.first_error = ""
        self.failing_since: Optional[datetime] = None
        # Whether the latest failure was DTEK's site not serving us (SiteDown), which is waited out.
        self.site_down = False
        # The run of our own failures at the end of the current streak (anything but SiteDown): since when, and
        # the error it began with.
        self.ours_since: Optional[datetime] = None
        self.ours_error = ""
        self._warned_time_types: set[str] = set()

    @property
    def name(self) -> str:
        return self.config.name

    def healthy(self, now: Optional[datetime] = None) -> bool:
        """Whether the data is fresh. The mini app says so; whether to raise an alarm is problem()'s call."""
        now = now or self._clock()
        return self.last_success is not None and now - self.last_success < STALE_AFTER

    def problem(self, now: Optional[datetime] = None) -> Optional[str]:
        """Why this source needs a look, or None. Stale data does, unless DTEK's own site is what keeps it stale:
        that is waited out up to SITE_DOWN_PATIENCE. The text stays the same while the trouble lasts, as it labels
        Grafana's alert."""
        now = now or self._clock()
        if self.healthy(now):
            return None
        since = _moment(self.last_success)
        stale_from = self.last_success or self.failing_since
        if self.failures and stale_from is not None and now - stale_from >= SITE_DOWN_PATIENCE:
            return f"no DTEK data since {since}{_kind(self.first_error)}"
        if self.failures and self.site_down:
            return None
        if self.ours_since is not None and now - self.ours_since < STALE_AFTER:
            return None
        return f"no DTEK data since {since}{_kind(self.ours_error or self.first_error)}"

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
        ok = await self._cycle(now)
        if not ok and self._edge is not None:
            # No news of DTEK to push, but the power is still watched here: the reserve must not take that over.
            await self._edge.alive(self.config, now)
        return ok

    async def _cycle(self, now: datetime) -> bool:
        try:
            raw = await self._site.fetch()
        except FetchError as e:
            self._failed("fetch", e, now)
            return False
        try:
            snapshot = build_snapshot(
                self.name, parse_house(raw.answer, self.config.building), parse_fact(raw.fact), raw.names, now
            )
        except ProtocolError as e:
            self._failed("protocol", e, now)
            return False
        except Exception as e:
            self._failed(f"parse ({type(e).__name__})", e, now)
            return False
        self._warn_time_types(raw.time_types)

        # Back after a while: the reserve may have messaged the chats meanwhile, so go on from what it sent.
        claimed = None
        if self._edge is not None and self._edge.away(self.name, now):
            claimed = await self._edge.claim(self.name)

        try:
            async with self._store.transaction() as tx:
                state = await tx.load_source_state(self.name)
                baseline = Baseline.from_dict(claimed or (state.baseline if state else None))
                detection = detect(baseline, snapshot, now, self.config.fingerprint)
                messages = self._router.for_events(self.config, detection.events, snapshot, now)
                await tx.save_source_state(self.name, snapshot.to_dict(), detection.baseline.to_dict(), now)
                await tx.save_legacy_schedule(self.config.schedule_id, legacy_row(snapshot, now.astimezone(KYIV_TZ).date()))
                await tx.enqueue(messages)
        except Exception as e:
            self._failed("store", e, now)
            return False

        if self.failures:
            logger.info(f"[{self.name}] recovered after {self.failures} failed cycles")
        self.failures, self.last_error, self.first_error = 0, "", ""
        self.failing_since, self.site_down, self.ours_since, self.ours_error = None, False, None, ""
        previous = self.latest
        self.latest, self.last_success = snapshot, now
        if self._on_schedule and previous is not None and (
                snapshot.schedule_updated != previous.schedule_updated
                or any(isinstance(e, ScheduleEvent) for e in detection.events)):
            self._on_schedule(snapshot.schedule_updated)
        if detection.initialized:
            logger.info(f"[{self.name}] baseline initialised, no notifications for the current state")
        for event in detection.events:
            logger.info(f"[{self.name}] event: {event}")
        if messages and self._on_messages:
            self._on_messages()
        if self._edge is not None:
            await self._edge.push(self.config, detection.baseline.to_dict(), now)
        return True

    def _failed(self, stage: str, error: Exception, now: datetime) -> None:
        self.failures += 1
        text = f"{stage}: {error}"
        down = isinstance(error, SiteDown)
        # Log the first failure, every change of error, and then every 10th repeat. DTEK's site being down is
        # only a warning: it is waited out.
        if self.failures == 1 or text != self.last_error or self.failures % 10 == 0:
            log = logger.warning if self.failures < 3 or down else logger.error
            log(f"[{self.name}] cycle failed ({self.failures} in a row): {text}")
        if down and not self.site_down:
            logger.warning(f"[{self.name}] DTEK's site is down or overloaded: waiting it out, "
                           f"alarm only after {SITE_DOWN_PATIENCE.total_seconds() / 3600:g} h without data")
        if self.failures == 1:
            self.first_error, self.failing_since = text, now
        if down:
            self.ours_since, self.ours_error = None, ""
        elif self.ours_since is None:
            # Trouble of ours right after good data counts from that data, as before; after the site was down,
            # from now.
            self.ours_since = self.last_success if self.failures == 1 and self.last_success else now
            self.ours_error = text
        self.site_down = down
        self.last_error = text

    def _warn_time_types(self, time_types: list) -> None:
        for value in unknown_time_types(time_types):
            if value not in self._warned_time_types:
                self._warned_time_types.add(value)
                logger.warning(f"[{self.name}] DTEK declares an unknown slot value {value!r}")
