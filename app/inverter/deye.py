"""The krem inverter, read from Deye Cloud's OpenAPI (developer.deyecloud.com).

The cloud only has what the inverter's Wi-Fi logger uploads (the krem one:
every ~72 s), and Deye has no push, so polls are timed to that rhythm: the
next one goes out when the next reading should have reached the cloud, with
one retry if it is late, then on to the following upload. That is ~1.2
calls per reading (~43k a month, as plain 60 s polling), leaving the 100k
quota room for other stations. device/latest is used rather than
station/latest, which Deye documents as refreshed at most every 5 minutes.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Protocol

import aiohttp

from app.config import DeyeConfig
from app.dtek.models import Snapshot
from app.notify.router import Router
from app.power.state import PowerState, PowerStatus, next_state
from app.storage.store import Store
from app.timeutil import KYIV_TZ, now_kyiv

logger = logging.getLogger(__name__)

# power_events row holding this inverter's grid state (row 1 is the home HA power state).
GRID_SLOT = 2


class DeyeError(Exception):
    pass


class DeyeRateLimited(DeyeError):
    pass


@dataclass(frozen=True)
class InverterReading:
    at: datetime                    # when the logger collected it
    soc: Optional[float]            # battery charge, %
    battery_w: Optional[float]      # positive while charging, negative while discharging
    load_w: Optional[float]         # what the house consumes
    pv_w: Optional[float]           # solar generation
    grid_w: Optional[float]         # drawn from the grid; negative when feeding in
    grid_v: Optional[float]
    grid_present: bool

    def to_dict(self, now: datetime, stale_after_s: float) -> dict:
        return {
            "at": self.at.isoformat(),
            # Time only, even for yesterday's reading: the stale flag is what says it is old.
            "time": self.at.astimezone(KYIV_TZ).strftime("%H:%M"),
            "stale": (now - self.at).total_seconds() > stale_after_s,
            "soc": self.soc,
            "battery_w": self.battery_w,
            "load_w": self.load_w,
            "pv_w": self.pv_w,
            "grid_w": self.grid_w,
            "grid_v": self.grid_v,
            "grid_present": self.grid_present,
        }


def parse_device_latest(item: dict, min_grid_voltage: float) -> InverterReading:
    """One entry of device/latest's deviceDataList. Single-phase inverters name grid points L1L2."""
    values = {p.get("key"): p.get("value") for p in item.get("dataList") or []}

    def num(*keys: str) -> Optional[float]:
        for key in keys:
            try:
                return float(values[key])
            except (KeyError, TypeError, ValueError):
                continue
        return None

    collected = item.get("collectionTime")
    if not collected:
        raise DeyeError(f"inverter {item.get('deviceSn')} has no collectionTime")
    battery = num("BatteryPower")
    grid_v = num("GridVoltageL1L2", "GridVoltageL1")
    return InverterReading(
        at=datetime.fromtimestamp(int(collected), KYIV_TZ),
        soc=num("SOC"),
        # Deye reports battery power negative while charging.
        battery_w=-battery if battery is not None else None,
        load_w=num("TotalConsumptionPower"),
        pv_w=num("TotalDCInputPower"),
        grid_w=num("TotalGridPower"),
        grid_v=grid_v,
        grid_present=grid_v is not None and grid_v >= min_grid_voltage,
    )


class DeyeCloud:
    # Tokens live 60 days; renew well before that.
    TOKEN_MARGIN_S = 24 * 3600

    def __init__(self, config: DeyeConfig, session: aiohttp.ClientSession):
        self._config = config
        self._session = session
        self._token: Optional[str] = None
        self._token_until = 0.0
        self._device_sn = config.device_sn

    async def _post(self, path: str, body: dict, token: Optional[str] = None) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with self._session.post(f"{self._config.base_url}/v1.0/{path}", json=body, headers=headers) as r:
            if r.status == 429:
                raise DeyeRateLimited(f"{path}: rate limited")
            if r.status >= 400:
                raise DeyeError(f"{path}: HTTP {r.status}")
            data = await r.json(content_type=None)
        if not data.get("success"):
            raise DeyeError(f"{path}: {data.get('code')} {data.get('msg')}")
        return data

    def _has_token(self) -> bool:
        return self._token is not None and time.monotonic() < self._token_until

    async def _auth(self) -> str:
        if self._has_token():
            return self._token
        data = await self._post(f"account/token?appId={self._config.app_id}", {
            "appSecret": self._config.app_secret,
            "email": self._config.email,
            "password": self._config.password_sha256,
        })
        self._token = data["accessToken"]
        self._token_until = time.monotonic() + max(0.0, float(data.get("expiresIn") or 0) - self.TOKEN_MARGIN_S)
        logger.info("Deye Cloud token obtained")
        return self._token

    async def _call(self, path: str, body: dict) -> dict:
        cached = self._has_token()
        try:
            return await self._post(path, body, await self._auth())
        except DeyeRateLimited:
            raise
        except DeyeError:
            if not cached:
                raise
            # A revoked token looks like any other failure; one retry with a new one.
            self._token = None
            return await self._post(path, body, await self._auth())

    async def _inverter(self) -> str:
        if self._device_sn is None:
            data = await self._call("station/device", {"stationIds": [self._config.station_id], "page": 1, "size": 20})
            inverters = [d for d in data.get("deviceListItems") or [] if d.get("deviceType") == "INVERTER"]
            if not inverters:
                raise DeyeError(f"station {self._config.station_id} has no inverter")
            self._device_sn = inverters[0]["deviceSn"]
            logger.info(f"Deye station {self._config.station_id}: inverter {self._device_sn}")
        return self._device_sn

    async def latest(self) -> Fetch:
        """The main inverter plus DEYE_BATTERIES, in one call: Deye's quota counts calls, not devices."""
        sn = await self._inverter()
        others = [other for _, other in self._config.batteries]
        data = await self._call("device/latest", {"deviceList": [sn, *others]})
        readings = {}
        for item in data.get("deviceDataList") or []:
            try:
                readings[item.get("deviceSn")] = parse_device_latest(item, self._config.min_grid_voltage)
            except DeyeError as e:
                # A battery inverter without data must not cost the main reading.
                logger.debug(f"Deye: skipping {item.get('deviceSn')}: {e}")
        main = readings.pop(sn, None)
        if main is None:
            raise DeyeError(f"device/latest returned nothing for {sn}")
        return Fetch(main, readings)


@dataclass(frozen=True)
class Fetch:
    main: InverterReading
    others: dict[str, InverterReading]     # by device SN


class Reader(Protocol):
    async def latest(self) -> Fetch: ...


class InverterMonitor:
    # Older than this, the mini app marks the reading as outdated.
    STALE_S = 15 * 60
    MAX_BACKOFF_S = 15 * 60
    # Upload rhythm until readings show the real one (the krem logger: 72 s, measured 16.10).
    CADENCE_S = 72.0
    # Deye Cloud has a reading 12-54 s after the logger took it (16.10); aim past most of that.
    INGEST_S = 40.0
    # One more look this long after a miss; past that, wait for the next upload.
    RETRY_S = 15.0
    MAX_RETRIES = 1
    MIN_WAIT_S = 5.0

    def __init__(
        self,
        config: DeyeConfig,
        clock: Callable[[], datetime] = now_kyiv,
        store: Optional[Store] = None,
        router: Optional[Router] = None,
        snapshot: Callable[[], Optional[Snapshot]] = lambda: None,
        on_messages: Optional[Callable[[], None]] = None,
    ):
        self.config = config
        self.source = config.source
        self._clock = clock
        # Grid lost/back messages for the source's chats; off without a store (tests of polling alone).
        self._store = store
        self._router = router
        self._snapshot = snapshot
        self._on_messages = on_messages
        self._grid: Optional[PowerState] = None
        self._grid_seen = False
        self.latest: Optional[InverterReading] = None
        self.batteries: dict[str, InverterReading] = {}     # by DEYE_BATTERIES name
        self.last_success: Optional[datetime] = None
        self.last_error = ""
        self.failures = 0
        self._gaps: deque[float] = deque(maxlen=5)
        self._misses = 0    # polls in a row that found no newer reading
        # For /health: how fresh readings are when seen, and what that costs in calls.
        self.calls = 0
        self.readings = 0
        self.last_lag_s: Optional[float] = None

    @property
    def cadence(self) -> float:
        """Seconds between uploads; a median, so an upload the logger skipped does not double it."""
        return statistics.median(self._gaps) if self._gaps else self.CADENCE_S

    def next_wait(self, now: datetime) -> float:
        """Until the next reading should be in the cloud; one short retry when it is late.

        After that, polls stay on the upload slots (one per cadence), so a
        skipped upload or an offline logger costs no more than normal polling.
        """
        if self.latest is None:
            return self.config.interval
        due = self.latest.at + timedelta(seconds=self.cadence + self.INGEST_S)
        until_due = (due - now).total_seconds()
        if until_due > 0:
            # Capped, so a logger clock running ahead cannot park the poller.
            return max(self.MIN_WAIT_S, min(until_due, self.cadence + self.INGEST_S))
        if self._misses <= self.MAX_RETRIES:
            return self.RETRY_S
        return max(self.MIN_WAIT_S, self.cadence - (-until_due % self.cadence))

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            cloud = DeyeCloud(self.config, session)
            while True:
                await asyncio.sleep(await self.poll(cloud))

    async def poll(self, reader: Reader) -> float:
        """One fetch; returns how long to wait before the next."""
        self.calls += 1
        try:
            fetch = await reader.latest()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.failures += 1
            self.last_error = f"{type(e).__name__}: {e}"
            if self.failures == 1 or self.failures % 10 == 0:
                logger.warning(f"Deye fetch failed ({self.failures} in a row): {self.last_error}")
            return min(self.config.interval * 2 ** min(self.failures, 4), self.MAX_BACKOFF_S)
        for name, sn in self.config.batteries:
            other = fetch.others.get(sn)
            if other is not None and (name not in self.batteries or other.at >= self.batteries[name].at):
                self.batteries[name] = other
        reading = fetch.main
        if self.latest is not None and reading.at > self.latest.at:
            gap = (reading.at - self.latest.at).total_seconds()
            if 20 <= gap <= 900:
                self._gaps.append(gap)
        if self.failures:
            logger.info(f"Deye fetch recovered after {self.failures} failures")
        now = self._clock()
        fresh = self.latest is None or reading.at > self.latest.at
        if fresh:
            self.readings += 1
            self.last_lag_s = round((now - reading.at).total_seconds())
            self._misses = 0
        else:
            self._misses += 1
        if self.latest is None or reading.at >= self.latest.at:
            self.latest = reading
        self.last_success = now
        self.last_error = ""
        self.failures = 0
        if fresh and self._store is not None:
            try:
                await self._follow_grid(reading, now)
            except Exception as e:
                # Not saved means not judged: the next reading compares against the stored state again.
                logger.warning(f"Deye grid state not saved: {type(e).__name__}: {e}")
        return self.next_wait(self.last_success)

    async def restore(self) -> None:
        if self._store is None:
            return
        async with self._store.transaction() as tx:
            self._grid = await tx.load_power_state(GRID_SLOT)
        if self._grid:
            logger.info(f"Deye grid state from DB: {self._grid.status.name} since {self._grid.since.astimezone(KYIV_TZ)}")

    async def _follow_grid(self, reading: InverterReading, now: datetime) -> None:
        """Message the source's chats when the inverter loses or regains the grid, with schedule context."""
        status = PowerStatus.ON if reading.grid_present else PowerStatus.OFF
        first = not self._grid_seen
        if self._grid is not None and status == self._grid.status:
            self._grid_seen = True
            return
        battery = f"{round(reading.soc)}%" if reading.soc is not None else "N/A"
        async with self._store.transaction() as tx:
            stored = await tx.load_power_state(GRID_SLOT)
            # A reading is always about a minute old, which is not a late notice, so it is judged at its
            # own time. Only the first one after a start may show a change made while the service was down.
            state, transition = next_state(stored, status, reading.at, reading.at, battery, from_snapshot=first)
            if state != stored:
                await tx.save_power_state(state, GRID_SLOT)
            messages = []
            if transition is not None and self.config.notify_grid and self._router is not None:
                messages = self._router.for_grid(self.source, transition, self._snapshot(), now)
            await tx.enqueue(messages)
        self._grid = state
        self._grid_seen = True
        if transition is None:
            logger.info(f"Deye grid state initialised: {state.status.name}")
            return
        logger.info(f"Deye: grid {'back' if transition.status is PowerStatus.ON else 'lost'} at {self.source} "
                    f"at {transition.at.astimezone(KYIV_TZ):%H:%M:%S} ({reading.grid_v} V)"
                    f"{' (noticed after a restart)' if transition.detected_late else ''}")
        if messages and self._on_messages:
            self._on_messages()

    def payload(self, now: datetime) -> Optional[dict]:
        return self.latest.to_dict(now, self.STALE_S) if self.latest else None

    def batteries_payload(self, now: datetime) -> list[dict]:
        """DEYE_BATTERIES in their configured order, for the batteries row of their source's mini app."""
        rows = []
        for name, _ in self.config.batteries:
            reading = self.batteries.get(name)
            if reading is not None:
                rows.append({
                    "name": name,
                    "soc": reading.soc,
                    "battery_w": reading.battery_w,
                    "stale": (now - reading.at).total_seconds() > self.STALE_S,
                })
        return rows
