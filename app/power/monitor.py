"""Watches the inverter's AC-input voltage in Home Assistant.

Uses `subscribe_entities`: right after (re)subscribing HA sends the current
state with its last_changed time, so an outage that began while we were
disconnected is still reported, with the moment it actually started.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

import aiohttp

from app.config import PowerConfig
from app.dtek.models import Snapshot
from app.energy import EnergyReading
from app.notify.router import Router
from app.power.state import (INPUT_NOT_CONNECTED, INPUT_UNKNOWN, PowerState, PowerStatus, next_state, parse_battery,
                             parse_number, parse_voltage)
from app.power.verdict import Actual
from app.storage.store import Store
from app.timeutil import KYIV_TZ, now_kyiv

logger = logging.getLogger(__name__)

SUBSCRIPTION_ID = 1


class HomeAssistantError(Exception):
    pass


class PowerMonitor:
    RECONNECT_MIN_S = 3.0
    RECONNECT_MAX_S = 60.0
    AUTH_TIMEOUT_S = 15.0
    # A dead connection is detected by websocket ping/pong, not by silence: while
    # the power is off the voltage stays at 0 and HA has nothing to send.
    PING_INTERVAL_S = 30.0
    # HA drops the websocket now and then and is back within seconds; the state holds that long meanwhile.
    LIVE_GRACE = timedelta(minutes=2)
    # 0 V with the input reading neither NOT_CONNECTED nor UNKNOWN (its sensor missing or late) is taken as an outage
    # after this long. A real outage brought NOT_CONNECTED within 2 s (16-17.11.2026).
    CONFIRM_S = 30.0

    def __init__(
        self,
        config: PowerConfig,
        store: Store,
        router: Router,
        snapshot: Callable[[], Optional[Snapshot]],
        clock: Callable[[], datetime] = now_kyiv,
        on_messages: Optional[Callable[[], None]] = None,
        energy: bool = False,
        on_energy: Optional[Callable[[], None]] = None,
    ):
        self._config = config
        self._store = store
        self._router = router
        self._snapshot = snapshot
        self._clock = clock
        self._on_messages = on_messages
        # Queue each change of the power for energy.win (app/energy.py) with the power state, and say so.
        self._energy = energy
        self._on_energy = on_energy
        self._state: Optional[PowerState] = None
        self._battery = "N/A"
        # For the mini app's batteries row; HA pushes every change, so these are live.
        self.battery_soc: Optional[float] = None
        self.battery_w: Optional[float] = None
        # The AC input's last reading; None while HA is away, as it may have changed meanwhile.
        self.voltage: Optional[float] = None
        self.connected = False
        self._lost_at: Optional[datetime] = None
        self._warned_unavailable = False
        self._input_source: Optional[str] = None
        # A 0 V reading waiting for the input source to tell an outage from a GX without data:
        # (its last_changed, whether it came in a snapshot), and the fallback that decides it after CONFIRM_S.
        self._low: Optional[tuple[datetime, bool]] = None
        self._confirm: Optional[asyncio.Task] = None

    @property
    def state(self) -> Optional[PowerState]:
        return self._state

    @property
    def source(self) -> str:
        return self._config.source

    @property
    def lost_at(self) -> Optional[datetime]:
        """When the last connection to HA was lost; None if it never connected."""
        return self._lost_at

    def actual(self, now: datetime) -> Optional[Actual]:
        """The home's power for the mini app's status; None until HA has said it, while HA is away or while the
        sensor has no value."""
        state = self._state
        if state is None or self._warned_unavailable:
            return None
        if not self.connected and (self._lost_at is None or now - self._lost_at > self.LIVE_GRACE):
            return None
        return Actual(state.status is PowerStatus.ON, state.since, "home_assistant")

    def energy_reading(self, now: datetime) -> Optional[EnergyReading]:
        """The power as it is now, for energy.win's repeat; None unless HA is connected and has a decided reading."""
        state = self._state
        if state is None or not self.connected or self._warned_unavailable or self.voltage is None or self._low:
            return None
        return EnergyReading(state.status is PowerStatus.ON, self.voltage, now)

    def battery_payload(self) -> Optional[dict]:
        if self.battery_soc is None:
            return None
        return {"name": "home", "soc": self.battery_soc, "battery_w": self.battery_w, "stale": not self.connected}

    async def restore(self) -> None:
        async with self._store.transaction() as tx:
            self._state = await tx.load_power_state()
        if self._state:
            logger.info(f"Power state from DB: {self._state.status.name} since {self._state.since.astimezone(KYIV_TZ)}")

    async def run(self) -> None:
        delay = self.RECONNECT_MIN_S
        while True:
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Home Assistant connection lost: {type(e).__name__}: {e}")
            self._disconnected()
            if time.monotonic() - started > 60:
                delay = self.RECONNECT_MIN_S
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.RECONNECT_MAX_S)

    def _disconnected(self) -> None:
        if self.connected:
            self._lost_at = self._clock()
        self.connected = False
        self.voltage = None
        self._input_source = None
        self._drop_low()

    async def _session(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                self._config.ws_url, heartbeat=self.PING_INTERVAL_S, max_msg_size=8 * 1024 * 1024
            ) as ws:
                await self._authenticate(ws)
                await ws.send_json({
                    "id": SUBSCRIPTION_ID,
                    "type": "subscribe_entities",
                    "entity_ids": [self._config.voltage_entity, self._config.battery_entity,
                                   self._config.battery_power_entity]
                                  + ([self._config.input_source_entity] if self._config.input_source_entity else []),
                })
                self.connected = True
                logger.info("Connected to Home Assistant, subscribed to power sensors")
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        continue
                    await self._handle(msg.json())
                raise HomeAssistantError(f"websocket closed (code {ws.close_code})")

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=self.AUTH_TIMEOUT_S)
        if hello.get("type") != "auth_required":
            raise HomeAssistantError(f"unexpected greeting {hello.get('type')!r}")
        await ws.send_json({"type": "auth", "access_token": self._config.token})
        result = await asyncio.wait_for(ws.receive_json(), timeout=self.AUTH_TIMEOUT_S)
        if result.get("type") != "auth_ok":
            raise HomeAssistantError(f"authentication failed: {result.get('message', result.get('type'))}")

    async def _handle(self, message: dict) -> None:
        if message.get("id") != SUBSCRIPTION_ID:
            return
        if message.get("type") == "result":
            if not message.get("success"):
                raise HomeAssistantError(f"subscribe_entities failed: {message.get('error')}")
            return
        if message.get("type") != "event":
            return
        event = message.get("event") or {}
        added = event.get("a") or {}
        changed = event.get("c") or {}
        # Battery first, so a transition in the same message reports the current level; the input source before the
        # voltage, so a snapshot's 0 V is judged by the input it came with.
        entities = (self._config.battery_entity, self._config.battery_power_entity, self._config.input_source_entity,
                    self._config.voltage_entity)
        for entity in filter(None, entities):
            if entity in added:
                state = added[entity]
                await self._observe(entity, state.get("s"), state.get("lc") or state.get("lu"), from_snapshot=True)
            if entity in changed:
                plus = changed[entity].get("+") or {}
                if "s" in plus:
                    await self._observe(entity, plus["s"], plus.get("lc") or plus.get("lu"), from_snapshot=False)

    async def _observe(self, entity: str, value: object, changed_ts: Optional[float], from_snapshot: bool) -> None:
        if entity == self._config.battery_entity:
            self._battery = parse_battery(value)
            self.battery_soc = parse_number(value)
            return
        if entity == self._config.battery_power_entity:
            self.battery_w = parse_number(value)
            return
        if entity == self._config.input_source_entity:
            self._input_source = value if isinstance(value, str) else None
            await self._settle()
            return
        self.voltage = parse_number(value)
        status = parse_voltage(value, self._config.min_voltage)
        if status is None:
            self._drop_low()
            self._unusable(f"Voltage sensor has no usable value ({value!r})")
            return
        changed_at = datetime.fromtimestamp(changed_ts, KYIV_TZ) if changed_ts else self._clock()
        if status is PowerStatus.OFF and self._config.input_source_entity:
            self._low = (changed_at, from_snapshot)
            await self._settle()
            return
        self._drop_low()
        await self._take(status, changed_at, from_snapshot)

    async def _settle(self) -> None:
        """Decides a 0 V reading by the AC input: an outage once it reads NOT_CONNECTED, no reading at all while it
        reads UNKNOWN (the GX has no data from the inverter), otherwise an outage after CONFIRM_S."""
        if self._low is None:
            return
        if self._input_source == INPUT_UNKNOWN:
            self._cancel_confirm()
            self._unusable("The GX has no data from the inverter (input source UNKNOWN), so its 0 V is no outage")
            return
        if self._input_source == INPUT_NOT_CONNECTED:
            changed_at, from_snapshot = self._low
            self._drop_low()
            await self._take(PowerStatus.OFF, changed_at, from_snapshot)
            return
        if self._confirm is None:
            self._confirm = asyncio.create_task(self._confirm_later())

    async def _confirm_later(self) -> None:
        await asyncio.sleep(self.CONFIRM_S)
        self._confirm = None
        low, self._low = self._low, None
        if low is None:
            return
        logger.warning(f"0 V and the input source still {self._input_source!r} after {self.CONFIRM_S:.0f}s: "
                       f"taking it as an outage")
        try:
            await self._take(PowerStatus.OFF, *low)
        except Exception as e:
            logger.error(f"Power OFF after the input source wait failed: {type(e).__name__}: {e}")

    async def _take(self, status: PowerStatus, changed_at: datetime, from_snapshot: bool) -> None:
        self._warned_unavailable = False
        if self._state is not None and status == self._state.status:
            return
        await self.apply(status, changed_at, self._clock(), from_snapshot)

    def _unusable(self, why: str) -> None:
        if not self._warned_unavailable:
            logger.warning(f"{why}; ignoring until it recovers")
            self._warned_unavailable = True

    def _drop_low(self) -> None:
        self._low = None
        self._cancel_confirm()

    def _cancel_confirm(self) -> None:
        if self._confirm is not None:
            self._confirm.cancel()
            self._confirm = None

    async def apply(self, status: PowerStatus, changed_at: datetime, now: datetime, from_snapshot: bool) -> None:
        async with self._store.transaction() as tx:
            stored = await tx.load_power_state()
            state, transition = next_state(stored, status, changed_at, now, self._battery, from_snapshot)
            changed = state != stored
            if changed:
                await tx.save_power_state(state)
                if self._energy:
                    await tx.enqueue_energy([EnergyReading(state.status is PowerStatus.ON, self.voltage, state.since)])
            messages = self._router.for_power(transition, self._snapshot(), now) if transition else []
            await tx.enqueue(messages)
        self._state = state
        if changed and self._energy and self._on_energy:
            self._on_energy()
        if transition is None:
            logger.info(f"Power state initialised: {state.status.name}")
            return
        logger.info(f"Power {transition.status.name} at {transition.at.astimezone(KYIV_TZ):%H:%M:%S} "
                    f"after {transition.duration_seconds:.0f}s{' (noticed late)' if transition.detected_late else ''}")
        if messages and self._on_messages:
            self._on_messages()
