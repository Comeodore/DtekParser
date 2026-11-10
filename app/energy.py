"""Sends the home's power, as the power monitor sees the Victron's AC input, to energy.win.co.ua (ENERGY_URL).

What is sent: every change of the power (with its real time, also one noticed late), the power as it is now
every ENERGY_REPEAT_S (60) while Home Assistant is connected, and nothing while it is not: unknown is not "off".
The repeat keeps the site's "last record N min ago" fresh in an outage too. The voltage goes with the power on
only, and not on its every change: the Victron's jitters by a volt every second or two.

Readings wait in energy_outbox until the site takes them. A change of the power is written there in the same
transaction as the power state, so a restart or the internet being down never loses it, and a backlog goes out
oldest first in arrays of up to 500. The site takes at most 30 requests a minute.

Not part of the heartbeat: the site being away must not raise "DTEK service problem". /api/v1/health and
/metrics show the queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable, Mapping, Optional

import aiohttp

# Not in app/config.py: that file is also part of dtek-brain on Cloudflare, which has no use for this.
from app.config import ConfigError, Settings, _get
from app.timeutil import KYIV_TZ, now_kyiv

if TYPE_CHECKING:
    from app.storage.store import Store

logger = logging.getLogger(__name__)

MAX_VOLTAGE = 500.0


@dataclass(frozen=True)
class EnergyConfig:
    url: str
    token: str
    repeat_s: float = 60.0


def load_energy(env: Mapping[str, str], settings: Settings) -> Optional[EnergyConfig]:
    """ENERGY_URL + ENERGY_TOKEN (+ ENERGY_REPEAT_S); None when not set."""
    url, token = _get(env, "ENERGY_URL"), _get(env, "ENERGY_TOKEN")
    if url is None and token is None:
        return None
    if url is None or token is None:
        raise ConfigError("ENERGY_URL and ENERGY_TOKEN go together")
    if not url.startswith(("https://", "http://")):
        raise ConfigError(f"ENERGY_URL={url!r} is not an http(s) URL")
    if settings.power is None:
        raise ConfigError("ENERGY_URL needs the Home Assistant power monitor (HA_WS_URL)")
    try:
        repeat = float(_get(env, "ENERGY_REPEAT_S", "60"))
    except ValueError as e:
        raise ConfigError("ENERGY_REPEAT_S is not a number") from e
    if repeat < 30:
        raise ConfigError("ENERGY_REPEAT_S must be at least 30")
    return EnergyConfig(url=url, token=token, repeat_s=repeat)


@dataclass(frozen=True)
class EnergyReading:
    power: bool
    voltage: Optional[float]
    at: datetime

    def payload(self) -> dict:
        body: dict = {"power": self.power}
        # With the power off the voltage says nothing (the site lists "0 V" under the outage). The site refuses a
        # whole request over one voltage outside 0-500 V; the power alone still counts.
        if self.power and self.voltage is not None and 0 <= self.voltage <= MAX_VOLTAGE:
            body["voltage"] = round(self.voltage, 1)
        body["ts"] = self.at.astimezone(KYIV_TZ).isoformat(timespec="seconds")
        return body


class RetryLater(Exception):
    def __init__(self, message: str, delay: Optional[float] = None):
        super().__init__(message)
        self.delay = delay


class Rejected(Exception):
    """400: the site refused the readings themselves; sending them again cannot help."""


def _message(text: str) -> str:
    try:
        return str(json.loads(text).get("message") or text)[:300]
    except (ValueError, AttributeError):
        return text[:300]


def _seconds(header: Optional[str]) -> Optional[float]:
    try:
        return max(float(header), 1.0) if header else None
    except ValueError:
        return None


class EnergyFeed:
    BATCH = 500             # the site's limit per request
    # The site takes 30 requests a minute; 2 s apart would be exactly that, so a little under.
    MIN_GAP_S = 2.5
    POLL_S = 5.0
    TIMEOUT_S = 15.0
    BACKOFF_MIN_S = 5.0
    BACKOFF_MAX_S = 300.0
    RATE_LIMITED_S = 60.0   # a 429 without Retry-After
    # A rejected token stays rejected until .env is fixed and the service restarted; asked again now and then.
    UNAUTHORIZED_S = 600.0
    KEEP = timedelta(days=7)
    PURGE_EVERY = timedelta(hours=1)

    def __init__(
        self,
        config: EnergyConfig,
        store: Store,
        current: Callable[[datetime], Optional[EnergyReading]],
        clock: Callable[[], datetime] = now_kyiv,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._config = config
        self._store = store
        self._current = current
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._wake = asyncio.Event()
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_post: Optional[float] = None
        self._retry_at: Optional[float] = None
        self._attempts = 0
        self._last_report: Optional[datetime] = None
        self._last_purge: Optional[datetime] = None
        # For /api/v1/health and /metrics; counts since the start.
        self.sent = 0
        self.rejected = 0
        self.failures = 0
        self.pending: Optional[int] = None
        self.last_success: Optional[datetime] = None
        self.last_error: Optional[str] = None
        # The newest reading the site took: what it shows now.
        self.last_taken: Optional[EnergyReading] = None

    def noted(self) -> None:
        """A change of the power was queued (by the power monitor): send it now, repeat a full interval later."""
        self._last_report = self._clock()
        self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await self.report_if_due()
                await self.flush()
                await self._purge_if_due()
                self.pending = await self._store.count_pending_energy()
            except Exception as e:
                logger.error(f"energy.win cycle failed: {type(e).__name__}: {e}")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.POLL_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def report_if_due(self) -> bool:
        """Queues the power as it is now once ENERGY_REPEAT_S passed since the last reading; not while it is unknown."""
        now = self._clock()
        if self._last_report is not None and (now - self._last_report).total_seconds() < self._config.repeat_s:
            return False
        reading = self._current(now)
        if reading is None:
            return False
        async with self._store.transaction() as tx:
            await tx.enqueue_energy([reading])
        self._last_report = now
        return True

    async def flush(self) -> int:
        """Sends what waits, oldest first, until all of it is sent or the site fails; returns how many it took."""
        taken = 0
        while self._retry_at is None or self._monotonic() >= self._retry_at:
            batch = await self._store.pending_energy(self.BATCH)
            if not batch:
                break
            ids = [i for i, _ in batch]
            body = [r.payload() for _, r in batch]
            await self._pace()
            try:
                received = await self._post(body)
            except Rejected as e:
                self.rejected += len(ids)
                self.last_error = f"HTTP 400: {e}"
                logger.error(f"energy.win refused {len(ids)} readings, dropped: {e}; first {body[0]}")
                await self._store.mark_energy_failed(ids, str(e), self._clock())
                continue
            except RetryLater as e:
                self._attempts += 1
                self.failures += 1
                delay = e.delay if e.delay is not None else min(
                    self.BACKOFF_MIN_S * 2 ** (self._attempts - 1), self.BACKOFF_MAX_S)
                self._retry_at = self._monotonic() + delay
                if str(e) != self.last_error:
                    logger.warning(f"energy.win did not take {len(ids)} readings: {e}; trying again in {delay:.0f}s")
                self.last_error = str(e)
                break
            await self._store.mark_energy_sent(ids, self._clock())
            if received != len(ids):
                logger.warning(f"energy.win took {received} of {len(ids)} readings")
            if self._attempts:
                logger.info(f"energy.win takes readings again after {self._attempts} failed tries")
            self._attempts, self._retry_at, self.last_error = 0, None, None
            self.sent += len(ids)
            taken += len(ids)
            self.last_success = self._clock()
            newest = max((r for _, r in batch), key=lambda r: r.at)
            if self.last_taken is None or newest.at >= self.last_taken.at:
                self.last_taken = newest
            if len(ids) > 1:
                logger.info(f"energy.win took {len(ids)} readings that had waited")
        return taken

    async def _pace(self) -> None:
        if self._last_post is not None:
            wait = self.MIN_GAP_S - (self._monotonic() - self._last_post)
            if wait > 0:
                await self._sleep(wait)
        self._last_post = self._monotonic()

    async def _post(self, body: list[dict]) -> int:
        """The number of readings the site says it took."""
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.TIMEOUT_S))
        try:
            async with self._session.post(self._config.url, json=body,
                                          headers={"Authorization": f"Bearer {self._config.token}"}) as r:
                status, text, retry_after = r.status, await r.text(), r.headers.get("Retry-After")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise RetryLater(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__) from e
        if status == 200:
            try:
                received = json.loads(text).get("received")
            except (ValueError, AttributeError):
                received = None
            return received if isinstance(received, int) else len(body)
        if status == 400:
            raise Rejected(_message(text))
        if status == 401:
            raise RetryLater("HTTP 401: the token is refused (ENERGY_TOKEN)", self.UNAUTHORIZED_S)
        if status == 429:
            raise RetryLater("HTTP 429: too many requests", _seconds(retry_after) or self.RATE_LIMITED_S)
        raise RetryLater(f"HTTP {status}: {_message(text)}")

    async def _purge_if_due(self) -> None:
        now = self._clock()
        if self._last_purge is not None and now - self._last_purge < self.PURGE_EVERY:
            return
        self._last_purge = now
        expired = await self._store.expire_energy(now - self.KEEP, now)
        if expired:
            logger.warning(f"energy.win: dropped {expired} readings it did not take for {self.KEEP.days} days")
        await self._store.purge_energy(now - self.KEEP)

    def status(self) -> dict:
        retry_in = self._retry_at - self._monotonic() if self._retry_at is not None else None
        return {
            "pending": self.pending,
            "sent": self.sent,
            "rejected": self.rejected,
            "failures": self.failures,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_error": self.last_error,
            "retry_in_s": round(retry_in) if retry_in is not None and retry_in > 0 else None,
        }

    def exposition(self) -> str:
        lines = ["# HELP energy_feed_pending Readings waiting for energy.win",
                 "# TYPE energy_feed_pending gauge"]
        if self.pending is not None:
            lines.append(f"energy_feed_pending {self.pending}")
        for name, help_, value in (
            ("energy_feed_sent_total", "Readings energy.win took since the start", self.sent),
            ("energy_feed_rejected_total", "Readings energy.win refused (HTTP 400), dropped", self.rejected),
            ("energy_feed_failures_total", "Requests to energy.win that failed and are retried", self.failures),
        ):
            lines += [f"# HELP {name} {help_}", f"# TYPE {name} counter", f"{name} {value}"]
        if self.last_success is not None:
            lines += ["# HELP energy_feed_last_success_timestamp_seconds When energy.win last took readings",
                      "# TYPE energy_feed_last_success_timestamp_seconds gauge",
                      f"energy_feed_last_success_timestamp_seconds {self.last_success.timestamp()}"]
        reading = self.last_taken
        if reading is not None:
            # Stamped with the time it was measured, as the Deye readings are: a scrape with honor_timestamps
            # stores one point per reading, and when no new one is taken the series stop rather than repeat.
            ms = int(reading.at.timestamp() * 1000)
            lines += ["# HELP energy_feed_power 1 while the newest reading energy.win took says the power is there",
                      "# TYPE energy_feed_power gauge",
                      f"energy_feed_power {1 if reading.power else 0} {ms}"]
            if reading.voltage is not None:
                lines += ["# HELP energy_feed_voltage_volts The AC input voltage in the newest reading energy.win took",
                          "# TYPE energy_feed_voltage_volts gauge",
                          f"energy_feed_voltage_volts {reading.voltage!r} {ms}"]
        return "\n".join(lines) + "\n"

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
