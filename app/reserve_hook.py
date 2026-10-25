"""Tells deye-reserve (DEYE_RESERVE_URL) that krem's schedule or grid changed, so it replans soon,
and asks it for today's sun forecast and, for the owner, its TOU tables for the krem mini app.

Fire-and-forget: a slow or absent deye-reserve never holds up a collector
cycle or a grid message, and it has a poll of its own as a fallback.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional, Sequence

import aiohttp

from app.timeutil import KYIV_TZ, MINUTES_IN_DAY

logger = logging.getLogger(__name__)


class ReserveHook:
    TIMEOUT_S = 5.0
    # The forecast changes twice a day; the mini app refreshes far more often.
    SUN_FRESH_S = 300.0
    # The plan changes at most every few minutes.
    PLAN_FRESH_S = 30.0
    GET_TIMEOUT_S = 2.0

    def __init__(self, url: Optional[str]):
        self.url = url.rstrip("/") if url else None
        self.sent = 0
        self.failures = 0
        self._tasks: set[asyncio.Task] = set()
        self._cache: dict[str, tuple[float, Optional[dict]]] = {}

    async def sun(self) -> Optional[dict]:
        """Today's PV forecast from deye-reserve's /sun ({"p50_kwh", "low_kwh", ...}), cached; None if not there."""
        return await self._get("sun", self.SUN_FRESH_S)

    async def plan(self) -> Optional[dict]:
        """deye-reserve's /plan: every day's table, when tomorrow's takes over, what the inverter took and why;
        cached; None if not there."""
        return await self._get("plan", self.PLAN_FRESH_S)

    async def _get(self, path: str, fresh_s: float) -> Optional[dict]:
        if not self.url:
            return None
        until, body = self._cache.get(path, (0.0, None))
        if time.monotonic() < until:
            return body
        body = None
        try:
            timeout = aiohttp.ClientTimeout(total=self.GET_TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.url}/{path}") as r:
                    if r.status == 200:
                        body = await r.json(content_type=None)
        except Exception as e:
            logger.debug(f"deye-reserve {path} not reached: {type(e).__name__}: {e}")
        self._cache[path] = (time.monotonic() + fresh_s, body)
        return body

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


# ---- deye-reserve's tables for the owner, in the krem mini app ----

# deye-reserve replans every hour: a plan older than this is a sign it stopped.
PLAN_STALE = timedelta(hours=2)


def ranges(slots: Sequence[Sequence]) -> list[dict]:
    """A day's TOU slots ([start minute, SOC, grid charge]) as ranges: slots in a row with the same target and
    charge flag are one, and the last one runs on past midnight into the first when they are the same (23:00 70%
    charged + 00:00 70% charged is one night, 23-07). Minutes of the day; an end before the start is past
    midnight, 1440 is midnight itself."""
    out: list[dict] = []
    for start, soc, charge in sorted((int(a), int(b), bool(c)) for a, b, c in slots):
        if out and (out[-1]["soc"], out[-1]["charge"]) == (soc, charge):
            continue
        if out:
            out[-1]["end"] = start
        out.append({"start": start, "end": None, "soc": soc, "charge": charge})
    if not out:
        return out
    if len(out) == 1:
        return [{**out[0], "start": 0, "end": MINUTES_IN_DAY}]
    out[-1]["end"] = out[0]["start"] or MINUTES_IN_DAY
    if (out[0]["soc"], out[0]["charge"]) == (out[-1]["soc"], out[-1]["charge"]):
        out[-1]["end"] = out.pop(0)["end"]
    return out


def _covers(r: dict, minute: int) -> bool:
    return r["start"] <= minute < r["end"] if r["start"] < r["end"] else minute >= r["start"] or minute < r["end"]


def _slots(table: Optional[list]) -> Optional[list]:
    return [[s["start"], s["soc"], s["grid_charge"]] for s in table] if table else None


def _when(at: datetime, now: datetime) -> str:
    at = at.astimezone(KYIV_TZ)
    return f"{at:%H:%M}" if at.date() == now.astimezone(KYIV_TZ).date() else f"{at:%d.%m %H:%M}"


def _dates(first: date, last: date) -> str:
    if first == last:
        return f"{first:%d.%m}"
    return f"{first:%d}–{last:%d.%m}" if first.month == last.month else f"{first:%d.%m}–{last:%d.%m}"


def charge_table(plan: dict, now: datetime) -> dict:
    """What the owner sees of deye-reserve's /plan: the table the inverter holds now, as ranges with the one now
    marked; tomorrow's as well when it differs, with the time it takes over; whether the inverter has it (or
    why not); and the last reason a shown table changed."""
    local = now.astimezone(KYIV_TZ)
    today, tomorrow = local.date(), local.date() + timedelta(days=1)
    tables = {date.fromisoformat(d): _slots(t) for d, t in (plan.get("tables") or {}).items()}
    switch_at = datetime.fromisoformat(plan["switch_at"]) if plan.get("switch_at") else None
    differ = tomorrow in tables and tables[tomorrow] != tables.get(today)
    # The day whose table the inverter holds: tomorrow's once it took over and differs from today's.
    current = tomorrow if differ and switch_at is not None and switch_at <= now else today
    minute = local.hour * 60 + local.minute

    def block(day: date, kind: str, last: Optional[date] = None, starts: Optional[str] = None) -> dict:
        rows = ranges(tables[day])
        if day == current:
            for r in rows:
                r["now"] = _covers(r, minute)
        return {"kind": kind, "dates": _dates(day, last or day), "current": day == current, "from": starts,
                "ranges": rows}

    blocks, shown = [], []
    if current == tomorrow:
        blocks, shown = [block(tomorrow, "tomorrow")], [tomorrow]
    elif today in tables and differ:
        starts = f"{switch_at.astimezone(KYIV_TZ):%H:%M}" if switch_at is not None else None
        blocks, shown = [block(today, "today"), block(tomorrow, "tomorrow", starts=starts)], [today, tomorrow]
    elif today in tables and tomorrow in tables:
        blocks, shown = [block(today, "both", last=tomorrow)], [today, tomorrow]
    elif today in tables:
        blocks, shown = [block(today, "today")], [today]

    held = tables.get(current)
    last_write = plan.get("last_write") or {}
    if held is None:
        status = None
    elif held == _slots(plan.get("written_table")):
        status = "written"
    elif not plan.get("write"):
        status = "off"
    else:
        status = "failed" if last_write.get("ok") is False else "pending"

    why = None
    noted = [(datetime.fromisoformat(w["at"]), w["reasons"]) for d, w in (plan.get("why") or {}).items()
             if date.fromisoformat(d) in shown and w.get("reasons")]
    if noted:
        at, reasons = max(noted, key=lambda n: n[0])
        why = {"at": _when(at, now), "reasons": reasons}
    at = datetime.fromisoformat(plan["at"])
    return {
        "at": _when(at, now),
        "stale": now - at > PLAN_STALE,
        "status": status,
        "fallback": plan.get("fallback"),
        "kept_from": _when(datetime.fromisoformat(plan["kept_from"]), now) if plan.get("kept_from") else None,
        "blocks": blocks,
        "why": why,
    }
