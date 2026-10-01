"""Watches the inverter's AC-input voltage in Home Assistant.

Uses `subscribe_entities`: right after (re)subscribing HA sends the current
state with its last_changed time, so an outage that began while we were
disconnected is still reported, with the moment it actually started.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable, Optional

import aiohttp

from app.config import PowerConfig
from app.dtek.models import Snapshot
from app.notify.router import Router
from app.power.state import PowerState, PowerStatus, next_state, parse_battery, parse_voltage
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

    def __init__(
        self,
        config: PowerConfig,
        store: Store,
        router: Router,
        snapshot: Callable[[], Optional[Snapshot]],
        clock: Callable[[], datetime] = now_kyiv,
        on_messages: Optional[Callable[[], None]] = None,
        on_alive: Optional[Callable[[], None]] = None,
    ):
        self._config = config
        self._store = store
        self._router = router
        self._snapshot = snapshot
        self._clock = clock
        self._on_messages = on_messages
        self._on_alive = on_alive
        self._state: Optional[PowerState] = None
        self._battery = "N/A"
        self.connected = False
        self._warned_unavailable = False

    @property
    def state(self) -> Optional[PowerState]:
        return self._state

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
            self.connected = False
            if time.monotonic() - started > 60:
                delay = self.RECONNECT_MIN_S
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.RECONNECT_MAX_S)

    async def watchdog(self) -> None:
        """Heartbeat while subscribed to Home Assistant."""
        while True:
            await asyncio.sleep(15)
            if self.connected and self._on_alive:
                self._on_alive()

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
                    "entity_ids": [self._config.voltage_entity, self._config.battery_entity],
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
        # Battery first, so a transition in the same message reports the current level.
        for entity in (self._config.battery_entity, self._config.voltage_entity):
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
            return
        status = parse_voltage(value, self._config.min_voltage)
        if status is None:
            if not self._warned_unavailable:
                logger.warning(f"Voltage sensor has no usable value ({value!r}), ignoring until it recovers")
                self._warned_unavailable = True
            return
        self._warned_unavailable = False
        if self._state is not None and status == self._state.status:
            return
        now = self._clock()
        changed_at = datetime.fromtimestamp(changed_ts, KYIV_TZ) if changed_ts else now
        await self.apply(status, changed_at, now, from_snapshot)

    async def apply(self, status: PowerStatus, changed_at: datetime, now: datetime, from_snapshot: bool) -> None:
        async with self._store.transaction() as tx:
            stored = await tx.load_power_state()
            state, transition = next_state(stored, status, changed_at, now, self._battery, from_snapshot)
            if state != stored:
                await tx.save_power_state(state)
            messages = self._router.for_power(transition, self._snapshot(), now) if transition else []
            await tx.enqueue(messages)
        self._state = state
        if transition is None:
            logger.info(f"Power state initialised: {state.status.name}")
            return
        logger.info(f"Power {transition.status.name} at {transition.at.astimezone(KYIV_TZ):%H:%M:%S} "
                    f"after {transition.duration_seconds:.0f}s{' (noticed late)' if transition.detected_late else ''}")
        if messages and self._on_messages:
            self._on_messages()
