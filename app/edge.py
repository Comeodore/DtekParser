"""The reserve on Cloudflare (edge/): it reads DTEK on its own and messages the chats while this service is away.

The two take turns and never message a source's chats at the same time. While this service runs it does it all
itself, and the reserve only notes what it is told: it uses none of the Cloudflare account's free Durable Object
time, which airdanger needs.

- DTEK's news. After each good cycle a source pushes its baseline, bot and chats to the reserve; the reserve
  takes a source over only once those pushes have stopped for its lease (5 minutes). A source that has not pushed
  for AWAY_AFTER first claims the source back: the reserve hands over the baseline it went on with, so the
  difference reported next starts from what the reserve already sent and nothing is sent twice.
- The power gone or back. This service sends these while it runs. The reserve watches the power only once it has
  heard nothing at all from this service for its lease: no push, and after a failed cycle no sign of life either
  (`alive`, so a service that cannot read DTEK still keeps the power). Both carry the power state this service
  holds for the source (`power`), which the reserve goes on from. On a start, before the power is watched again,
  this service claims the power back and keeps the state the reserve left (take_back_power), so a change the
  reserve has reported is not reported again.
- The mini app. The reserve stands in for it while this service's own is down. krem's pushes also carry what its
  inverter diagram needs from here (`inverter`): today's grid by tariff so far and deye-reserve's sun forecast for
  the days ahead, so the stand-in page splits the grid into night and day and shows the forecast as this one does.

A failed call leaves the service working as without the reserve: it would rather repeat a message than lose one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Iterable, Mapping, Optional

import aiohttp

from app.config import SourceConfig
from app.energy import EnergyReading
from app.power.state import PowerState, PowerStatus
from app.storage.store import Store
from app.timeutil import KYIV_TZ

logger = logging.getLogger(__name__)

# Well inside the reserve's lease, so a source that is back claims before the reserve could have sent anything new.
AWAY_AFTER = timedelta(minutes=3)
# A failed cycle says this service is alive at most this often; a failing source retries every 15 s at first.
ALIVE_EVERY = timedelta(seconds=60)
TIMEOUT = aiohttp.ClientTimeout(total=5)


async def _nothing(source: str) -> Optional[dict]:
    return None


def power_body(state: Optional[PowerState]) -> Optional[dict]:
    """A power state as the reserve keeps it: on, and since when (ms)."""
    if state is None:
        return None
    return {"on": state.status is PowerStatus.ON, "since": int(state.since.timestamp() * 1000)}


class Edge:
    def __init__(self, url: str, key: str, bots: Mapping[str, str]):
        self.url = url.rstrip("/")
        self._key = key
        self._bots = dict(bots)
        self._session: Optional[aiohttp.ClientSession] = None
        self._pushed: dict[str, datetime] = {}
        self._heard: dict[str, datetime] = {}
        self._errors: dict[str, str] = {}
        # Per source, what the reserve needs to read Deye itself for the mini app it stands in for (set by Service).
        self.deye: Callable[[str], Optional[dict]] = lambda source: None
        # Per source, the power state this service holds, which the reserve goes on from (set by Service).
        self.power: Callable[[str], Optional[PowerState]] = lambda source: None
        # Per source, what the reserve's stand-in page needs to draw the inverter as here (set by Service): today's
        # grid by tariff so far (`grid_day`, GridDay.payload) and deye-reserve's sun forecast by day (`sun`).
        self.inverter: Callable[[str], Awaitable[Optional[dict]]] = _nothing

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
        power = power_body(self.power(config.name))
        if power is not None:
            body["power"] = power
        deye = self.deye(config.name)
        if deye is not None:
            body["deye"] = deye
        try:
            inverter = await self.inverter(config.name)
        except Exception as e:
            logger.warning(f"[{config.name}] the inverter's day not pushed: {type(e).__name__}: {e}")
            inverter = None
        if inverter is not None:
            body["inverter"] = inverter
        if await self._post(config.name, "home", body) is not None:
            self._pushed[config.name] = now
            self._heard[config.name] = now

    async def alive(self, config: SourceConfig, now: datetime) -> None:
        """After a failed cycle: there is no news of DTEK to push, but this service is here and keeps the power, so
        the reserve must not start sending those messages. At most once every ALIVE_EVERY."""
        last = self._heard.get(config.name)
        if last is not None and now - last < ALIVE_EVERY:
            return
        body: dict = {"alive": True}
        power = power_body(self.power(config.name))
        if power is not None:
            body["power"] = power
        if await self._post(config.name, "home", body) is not None:
            self._heard[config.name] = now

    async def claim_power(self, source: str) -> Optional[PowerState]:
        """The power state the reserve left, when it watched the power while this service was away; None when it
        did not (or cannot be reached). The reserve stops watching it either way."""
        answer = await self._post(source, "claim", {"power": True})
        state = answer.get("power") if answer else None
        if not state:
            return None
        status = PowerStatus.ON if state["on"] else PowerStatus.OFF
        since = datetime.fromtimestamp(state["since"] / 1000, KYIV_TZ)
        logger.warning(f"[{source}] the reserve watched the power while this service was away; going on from "
                       f"{status.name} since {since:%d.%m %H:%M:%S}")
        return PowerState(status, since)

    async def take_back_power(self, store: Store, slots: Iterable[tuple[str, int]],
                              energy_slot: Optional[int] = None) -> None:
        """On a start, before the power is watched again: each source's power (source, power_events slot) from the
        reserve, where it watched it meanwhile, so what it has reported is not reported again. A change it saw in
        energy_slot's power is queued for energy.win with its time, as the power monitor would have."""
        for source, slot in slots:
            state = await self.claim_power(source)
            if state is None:
                continue
            async with store.transaction() as tx:
                if slot == energy_slot:
                    stored = await tx.load_power_state(slot)
                    if stored is None or stored.status != state.status:
                        await tx.enqueue_energy([EnergyReading(state.status is PowerStatus.ON, None, state.since)])
                await tx.save_power_state(state, slot)

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
