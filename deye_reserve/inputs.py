"""What the planner works from, parsed from the raw answers: the DTEK schedule, the sun, the house's load."""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from deye_reserve.planner import Outage
from deye_reserve.timeutil import KYIV_TZ


class InputError(Exception):
    """An input is missing or unusable; the planner falls back to the safe table."""


# ---- DTEK schedule (dtek-service GET /api/v1/schedule) ----

HALVES = {
    "power_on": (False, False),
    "power_off": (True, True),
    "power_off_first_30": (True, False),
    "power_off_second_30": (False, True),
}


def _union(slots: Iterable[str]) -> str:
    """One hour over several lines: without power whenever any of them is off."""
    first = second = False
    for slot in slots:
        a, b = HALVES.get(slot, (True, True))      # unknown values: assume no power
        first, second = first or a, second or b
    return {v: k for k, v in HALVES.items()}[(first, second)]


@dataclass(frozen=True)
class Schedule:
    outages: tuple[Outage, ...]
    days: dict[date, tuple[str, ...]]      # our group's 24 hourly slots, by date
    tomorrow_published: bool
    updated: str                            # DTEK's "last updated" stamp
    group_found: bool


def parse_schedule(payload: dict, group: str, now: datetime, max_age: timedelta) -> Schedule:
    """Outages of `group` today and tomorrow, merged across midnight.

    Tomorrow without any outage counts as not published yet: DTEK publishes
    days with outages, and a quiet tomorrow cannot be told from a missing one.
    """
    if payload.get("schedules_suspended"):
        raise InputError("DTEK призупинив погодинні графіки")
    updated_at = payload.get("updated_at")
    if not updated_at:
        raise InputError("dtek-service ще не отримав графік")
    age = now - datetime.fromisoformat(updated_at)
    if age > max_age:
        raise InputError(f"графік не оновлювався {age.total_seconds() / 3600:.1f} год")
    today = now.astimezone(KYIV_TZ).date()
    by_date: dict[date, tuple[str, ...]] = {}
    found = True
    for day in payload.get("days") or []:
        lines = day.get("lines") or []
        if not lines:
            continue
        mine = [line for line in lines if line.get("group") == group]
        if not mine:
            # Not the expected group: every line the house hangs on counts.
            found = False
            mine = lines
        by_date[date.fromisoformat(day["date"])] = tuple(_union(line["slots"][h] for line in mine) for h in range(24))
    if today not in by_date:
        raise InputError("у графіку немає сьогоднішнього дня")
    tomorrow = today + timedelta(days=1)
    published = tomorrow in by_date and any(s != "power_on" for s in by_date[tomorrow])
    if not published:
        by_date.pop(tomorrow, None)
    halves: list[tuple[datetime, bool]] = []
    for d in sorted(by_date):
        midnight = datetime.combine(d, datetime.min.time(), KYIV_TZ)
        for h, slot in enumerate(by_date[d]):
            first, second = HALVES[slot]
            halves.append((midnight + timedelta(hours=h), first))
            halves.append((midnight + timedelta(hours=h, minutes=30), second))
    outages, start = [], None
    for at, off in halves:
        if off and start is None:
            start = at
        elif not off and start is not None:
            outages.append((start, at))
            start = None
    if start is not None:
        last = halves[-1][0] + timedelta(minutes=30)
        outages.append((start, last))
    return Schedule(tuple(outages), by_date, published, payload.get("schedule_updated") or "", found)


def changed_days(old: Optional[dict[str, list[str]]], new: dict[date, tuple[str, ...]], today: date) -> list[date]:
    """Days from today on whose outages appeared, changed or were withdrawn; days left behind do not count."""
    old = old or {}
    quiet = ["power_on"] * 24
    days = {d for d, slots in new.items() if d >= today and list(slots) != old.get(d.isoformat(), quiet)}
    days |= {date.fromisoformat(d) for d in old if date.fromisoformat(d) >= today and date.fromisoformat(d) not in new
             and old[d] != quiet}
    return sorted(days)


# ---- Sun (Open-Meteo) ----

# Hours that can have sun in Kyiv in any season; a forecast day needs all of them.
DAYLIGHT = set(range(4, 22))

@dataclass(frozen=True)
class Forecast:
    p50: dict[tuple[date, int], float]      # PV W by (Kyiv date, hour)
    pess: dict[tuple[date, int], float]
    fetched_at: datetime
    ensemble: bool

    def covers(self, days: Iterable[date]) -> bool:
        return all((d, h) in self.p50 for d in days for h in range(6, 19))

    def total_kwh(self, day: date, pessimistic: bool = False, from_hour: int = 0) -> float:
        src = self.pess if pessimistic else self.p50
        return sum(v for (d, h), v in src.items() if d == day and h >= from_hour) / 1000

    def to_dict(self) -> dict:
        def pack(src):
            return {f"{d.isoformat()}T{h:02d}": round(v, 1) for (d, h), v in src.items()}
        return {"p50": pack(self.p50), "pess": pack(self.pess), "fetched_at": self.fetched_at.isoformat(),
                "ensemble": self.ensemble}

    @classmethod
    def from_dict(cls, data: dict) -> "Forecast":
        def unpack(src):
            return {(date.fromisoformat(k[:10]), int(k[11:13])): v for k, v in src.items()}
        return cls(unpack(data["p50"]), unpack(data["pess"]), datetime.fromisoformat(data["fetched_at"]),
                   data.get("ensemble", False))


def _median(values: list[float]) -> float:
    return statistics.median(values)


def combine_forecast(multi: dict, ensemble: Optional[dict], pv_per_ghi: float, fetched_at: datetime) -> Forecast:
    """P50 and pessimistic PV per hour from Open-Meteo's multi-model and ECMWF-ensemble answers.

    Backtest 23.08-16.10 (day-ahead vs satellite): ECMWF IFS had the lowest
    error, the median of models never mixed up dark, mixed and sunny days,
    and the default best_match (ICON) under-forecast. So:
      P50  = mean(ECMWF, median of models)
      pess = min(ECMWF ensemble P10, median of models)
    A model with gaps on a day (UKMO beyond ~2 days) is left out for that day.
    Radiation is the mean of the preceding hour: the value stamped h+1 is PV hour h.
    Without the ensemble the pessimistic case is half the median.
    """
    hourly = multi.get("hourly") or {}
    times = hourly.get("time") or []
    models = [k for k in hourly if k != "time"]
    if not times or not models:
        raise InputError("Open-Meteo не повернув прогноз")
    ecmwf = next((k for k in models if k.endswith("ecmwf_ifs025")), None)

    def gen_hour(stamp: str) -> tuple[date, int]:
        t = datetime.fromisoformat(stamp) - timedelta(hours=1)
        return t.date(), t.hour

    by_day: dict[date, list[int]] = defaultdict(list)
    for i, stamp in enumerate(times):
        by_day[gen_hour(stamp)[0]].append(i)

    members: dict[str, list[Optional[float]]] = {}
    ens_index: dict[str, int] = {}
    if ensemble:
        eh = ensemble.get("hourly") or {}
        members = {k: v for k, v in eh.items() if k != "time"}
        ens_index = {t: i for i, t in enumerate(eh.get("time") or [])}

    p50, pess = {}, {}
    for day, idx in by_day.items():
        if not DAYLIGHT <= {gen_hour(times[i])[1] for i in idx}:
            continue        # a day cut by the range is not used
        usable = [m for m in models if all(hourly[m][i] is not None for i in idx)]
        if not usable:
            continue
        for i in idx:
            key = gen_hour(times[i])
            med = _median([hourly[m][i] for m in usable])
            mid = (med + hourly[ecmwf][i]) / 2 if ecmwf in usable else med
            low = med * 0.5
            j = ens_index.get(times[i])
            if j is not None:
                vals = sorted(v[j] for v in members.values() if v[j] is not None)
                if vals:
                    low = min(vals[min(int(len(vals) * 0.1), len(vals) - 1)], med)
            p50[key] = mid * pv_per_ghi
            pess[key] = low * pv_per_ghi
    if not p50:
        raise InputError("у прогнозі Open-Meteo немає повних днів")
    return Forecast(p50, pess, fetched_at, bool(members))


# ---- Load (Deye station/history/power) ----

@dataclass(frozen=True)
class LoadProfile:
    mean: dict[int, float]      # W by hour
    p80: dict[int, float]
    days: int
    last_full: Optional[date]   # last day the battery reached 99%+
    fetched_at: datetime

    def to_dict(self) -> dict:
        return {"mean": self.mean, "p80": self.p80, "days": self.days, "fetched_at": self.fetched_at.isoformat(),
                "last_full": self.last_full.isoformat() if self.last_full else None}

    @classmethod
    def from_dict(cls, data: dict) -> "LoadProfile":
        return cls({int(k): v for k, v in data["mean"].items()}, {int(k): v for k, v in data["p80"].items()},
                   data["days"], date.fromisoformat(data["last_full"]) if data.get("last_full") else None,
                   datetime.fromisoformat(data["fetched_at"]))


def load_profile(items: list[dict], first: date, last: date, fetched_at: datetime) -> LoadProfile:
    """Hourly mean and P80 of the house's consumption over [first, last] (Kyiv dates)."""
    by_hour: dict[int, list[float]] = defaultdict(list)
    seen: set[date] = set()
    last_full = None
    for item in items:
        t = datetime.fromtimestamp(float(item["timeStamp"]), KYIV_TZ)
        soc = item.get("batterySOC")
        if soc is not None and soc >= 99 and (last_full is None or t.date() > last_full):
            last_full = t.date()
        if not first <= t.date() <= last:
            continue
        load = item.get("consumptionPower")
        if load is None:
            continue
        by_hour[t.hour].append(float(load))
        seen.add(t.date())
    if len(by_hour) < 24 or len(seen) < 2:
        raise InputError(f"замало історії навантаження: {len(seen)} дн.")
    mean = {h: sum(v) / len(v) for h, v in sorted(by_hour.items())}
    p80 = {h: sorted(v)[int(len(v) * 0.8)] for h, v in sorted(by_hour.items())}
    return LoadProfile(mean, p80, len(seen), last_full, fetched_at)
