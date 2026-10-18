"""Picks the inverter's Time of Use tables for the hours ahead, one per calendar day.

A Deye TOU table is six slots (start time, target SOC, grid charge on/off)
that repeat every day. While the grid is there the battery is not discharged
below the slot's SOC, and with grid charge on it is charged up to it; during
an outage it runs down to the shutdown SOC whatever the slot says.

The plan covers the time from now to 23:00 of the next "tariff day" (the day
whose night is coming, or under way), so today and tomorrow are planned
together: the coming night's target is chosen as the cheapest one that is
still safe, and outages the night charge cannot cover get a just-in-time
charge right before them. Each day gets a table of its own with its own
charges, and the simulation switches tables at midnight; the inverter holds
one table, so today's is written now and tomorrow's from the moment it no
longer differs from today's for the rest of the day (23:00, when the night
slot starts, unless a charge of today runs past it).

"Safe" means the pessimistic case (P80 load x1.1, pessimistic sun) keeps the
battery at or above outage_min_soc through every scheduled outage. A
candidate is priced at 70% of the expected case (mean load, P50 sun) and 30%
of the pessimistic one, so a plan that only pays off when everything goes
right does not win; energy left in the battery at the end is worth what it
would cost to buy it at night.

Charging and discharging follow what this battery measured (09-10.2026): the
load gets 9.75 kWh per 100% of SOC, while filling 100% takes 12.3 kWh from the
grid or 11.2 kWh of PV surplus.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Mapping, Optional, Sequence, Union

from deye_reserve.timeutil import KYIV_TZ

MINUTES_IN_DAY = 24 * 60
SLOTS = 6
GRID = 15   # minutes; every slot starts on this grid


@dataclass(frozen=True)
class Params:
    capacity_kwh: float = 10.24
    efficiency: float = 0.95          # out of the battery: the load gets capacity x this per 100% of SOC
    charge_efficiency: float = 0.83   # into the battery from the grid (12.3 kWh per 100%)
    pv_efficiency: float = 0.91       # into the battery from PV surplus (11.2 kWh per 100%)
    charge_kw: float = 2.7            # grid charge, grid side (Grid Charge 50 A)
    pv_charge_kw: float = 5.0         # PV surplus the battery takes at most (Max A Charge 100 A)
    shutdown_soc: float = 20.0
    outage_min_soc: float = 25.0      # pessimistic floor during scheduled outages
    day_floor: int = 45               # kept by day: the reserve for unscheduled outages
    night_min: int = 45
    night_max: int = 95
    night_step: int = 5
    calibration_soc: int = 100        # once in a while the battery is charged full
    day_price: float = 4.32           # UAH/kWh
    night_price: float = 2.16
    night_start_h: int = 23
    night_end_h: int = 7
    slot_power_w: int = 6000
    safety_load_factor: float = 1.1
    pess_cost_weight: float = 0.3     # a candidate's price: this much of the pessimistic case, the rest expected
    step_min: int = 5
    charge_lead_min: int = 30         # just-in-time charges start this much earlier than needed
    charge_extra_soc: float = 2.5
    hysteresis_uah: float = 0.3       # a higher night target must be cheaper by more than this
    keep_previous_uah: float = 0.5    # the current tables stay unless the new ones save more


@dataclass(frozen=True)
class Slot:
    start: int          # minute of day
    soc: int
    grid_charge: bool

    @property
    def time(self) -> str:
        return f"{self.start // 60:02d}:{self.start % 60:02d}"


Table = tuple[Slot, ...]
Tables = Union[Table, Mapping[date, Table]]     # one table for every day, or one per Kyiv date
Outage = tuple[datetime, datetime]


def safe_table(params: Params, night: int = 90) -> Table:
    """What the inverter has had all along: night charge to 90%, 45% by day."""
    hold = params.day_floor
    return (Slot(0, night, True), Slot(7 * 60, hold, False), Slot(11 * 60, hold, False),
            Slot(15 * 60, hold, False), Slot(19 * 60, hold, False), Slot(23 * 60, night, True))


def slot_at(table: Sequence[Slot], minute: int) -> Slot:
    current = table[0]
    for slot in table:
        if slot.start > minute:
            break
        current = slot
    return current


def table_on(tables: Tables, day: date) -> Table:
    """The table in force on `day`: its own, or (past the last one planned) the last one, which stays in."""
    if not isinstance(tables, Mapping):
        return tables
    if day in tables:
        return tables[day]
    return tables[max(tables)] if day > max(tables) else tables[min(tables)]


def switch_minute(today: Sequence[Slot], tomorrow: Sequence[Slot]) -> int:
    """From this minute of today on, tomorrow's table does what today's does for the rest of the day."""
    m = MINUTES_IN_DAY
    while m > 0:
        a, b = slot_at(today, m - GRID), slot_at(tomorrow, m - GRID)
        if (a.soc, a.grid_charge) != (b.soc, b.grid_charge):
            break
        m -= GRID
    return m


def table_now(tables: Mapping[date, Table], now: datetime) -> tuple[Table, Optional[datetime]]:
    """The table for the inverter now, and when tomorrow's takes over (None: no tomorrow planned)."""
    today = local(now).date()
    t0, t1 = table_on(tables, today), tables.get(today + timedelta(days=1))
    if t1 is None:
        return t0, None
    at = datetime.combine(today, time(0), KYIV_TZ) + timedelta(minutes=switch_minute(t0, t1))
    return (t1 if local(now) >= at else t0), at


@dataclass(frozen=True)
class Inputs:
    now: datetime
    soc: float
    outages: tuple[Outage, ...]                     # scheduled, in time order
    load_mean: Mapping[int, float]                  # W by hour of day
    load_high: Mapping[int, float]                  # P80, W by hour of day (before the safety factor)
    pv_p50: Mapping[tuple[date, int], float]        # W by (Kyiv date, hour)
    pv_pess: Mapping[tuple[date, int], float]
    calibrate: bool = False


@dataclass
class Sim:
    soc_start: float
    soc_end: float = 0.0
    buy_day: float = 0.0        # kWh
    buy_night: float = 0.0
    curtailed: float = 0.0      # PV the battery could not take (zero export)
    unserved: float = 0.0       # load not covered during outages
    min_by: dict[int, float] = field(default_factory=dict)   # lowest SOC per outage index
    unserved_by: dict[int, float] = field(default_factory=dict)  # kWh not covered, per outage index
    soc_at: dict[datetime, float] = field(default_factory=dict)   # on the hour
    cost: float = 0.0
    cost_adj: float = 0.0


def local(t: datetime) -> datetime:
    return t.astimezone(KYIV_TZ)


def minute_of_day(t: datetime) -> int:
    lt = local(t)
    return lt.hour * 60 + lt.minute


def is_night(t: datetime, params: Params) -> bool:
    h = local(t).hour
    return h >= params.night_start_h or h < params.night_end_h


def window(now: datetime, params: Params) -> tuple[datetime, datetime]:
    """From now to 23:00 of the day whose night is coming or under way."""
    lt = local(now)
    day = lt.date() if lt.hour < params.night_end_h else lt.date() + timedelta(days=1)
    end = datetime.combine(day, time(params.night_start_h), KYIV_TZ)
    start = now - timedelta(minutes=lt.minute % params.step_min, seconds=lt.second, microseconds=lt.microsecond)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def simulate(tables: Tables, start: datetime, end: datetime, soc: float,
             load_w: Callable[[datetime], float], pv_w: Callable[[datetime], float],
             outages: Sequence[Outage], params: Params) -> Sim:
    """Battery and grid over [start, end) in steps; load_w/pv_w take Kyiv time and give W."""
    sim = Sim(soc_start=soc)
    h = params.step_min / 60
    cap, eta = params.capacity_kwh, params.efficiency
    t = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    while t < end:
        lt = local(t)
        if lt.minute == 0:
            sim.soc_at[lt] = soc
        load, pv = load_w(lt) / 1000, pv_w(lt) / 1000
        k = next((i for i, (a, b) in enumerate(outages) if a <= t < b), None)
        absent = k is not None
        slot = slot_at(table_on(tables, lt.date()), lt.hour * 60 + lt.minute)
        grid = 0.0
        surplus = pv - load
        took = 0.0
        if surplus >= 0:
            room = (100 - soc) / 100 * cap / params.pv_efficiency / h
            took = min(surplus, room, params.pv_charge_kw)
            soc += took * h * params.pv_efficiency / cap * 100
            sim.curtailed += (surplus - took) * h
        else:
            deficit = -surplus
            floor = params.shutdown_soc if absent else slot.soc
            avail = max(0.0, (soc - floor) / 100 * cap * eta / h)
            from_battery = min(deficit, avail)
            soc -= from_battery * h / eta / cap * 100
            rest = deficit - from_battery
            if absent:
                sim.unserved += rest * h
                sim.unserved_by[k] = sim.unserved_by.get(k, 0.0) + rest * h
            else:
                grid += rest
        if not absent and slot.grid_charge and soc < slot.soc:
            rate = min(params.charge_kw, max(0.0, params.pv_charge_kw - took))
            charge = min(rate, (slot.soc - soc) / 100 * cap / params.charge_efficiency / h)
            soc += charge * h * params.charge_efficiency / cap * 100
            grid += charge
        soc = min(soc, 100.0)
        if absent:
            sim.min_by[k] = min(sim.min_by.get(k, 100.0), soc)
        if is_night(t, params):
            sim.buy_night += grid * h
        else:
            sim.buy_day += grid * h
        t += timedelta(minutes=params.step_min)
    sim.soc_end = soc
    sim.cost = sim.buy_day * params.day_price + sim.buy_night * params.night_price
    sim.cost_adj = sim.cost - (soc - sim.soc_start) / 100 * cap / params.charge_efficiency * params.night_price
    return sim


def shortfall(sim: Sim, k: int, params: Params) -> float:
    """SOC points outage k misses in this run: below the floor, plus what ran out at shutdown."""
    below = max(0.0, params.outage_min_soc - sim.min_by.get(k, 100.0))
    return below + sim.unserved_by.get(k, 0.0) / params.efficiency / params.capacity_kwh * 100


@dataclass(frozen=True)
class Charge:
    """Grid charge to `soc` from `start` until the outage at `end` begins."""
    start: datetime
    end: datetime
    soc: int


def _inside(minute: int, a: int, b: int) -> bool:
    return a <= minute < b if a <= b else (minute >= a or minute < b)


def profile_table(night: int, charges: Sequence[Charge], params: Params, day: Optional[date] = None) -> list[Slot]:
    """Breakpoints of the target SOC over a day: night target, day floor, charges on top.

    With `day`, only the charges (or their parts) that fall on that date count; without it, every charge
    lands on its time of day."""
    if day is None:
        spans = [(minute_of_day(c.start), minute_of_day(c.end), c.soc) for c in charges]
    else:
        d0 = datetime.combine(day, time(0), KYIV_TZ)
        d1 = d0 + timedelta(days=1)
        spans = []
        for c in charges:
            a, b = max(local(c.start), d0), min(local(c.end), d1)
            if a < b:
                spans.append((minute_of_day(a), MINUTES_IN_DAY if b == d1 else minute_of_day(b), c.soc))

    def at(minute: int) -> tuple[int, bool]:
        if _inside(minute, params.night_start_h * 60, params.night_end_h * 60):
            soc, gc = night, True
        else:
            soc, gc = params.day_floor, False
        for a, b, target in spans:
            if a != b and _inside(minute, a, b):
                soc, gc = max(soc, target), True
        return soc, gc

    points, last = [], None
    for minute in range(0, MINUTES_IN_DAY, GRID):
        value = at(minute)
        if value != last:
            points.append(Slot(minute, *value))
            last = value
    return points


def pad(points: Sequence[Slot]) -> Table:
    """Six slots: split the longest stretch (at a whole hour when possible) until there are six."""
    slots = list(points)
    while len(slots) < SLOTS:
        ends = [s.start for s in slots[1:]] + [MINUTES_IN_DAY]
        length, i = max((e - s.start, i) for i, (s, e) in enumerate(zip(slots, ends)))
        mid = slots[i].start + length // 2
        for step in (60, GRID):
            if slots[i].start < mid - mid % step:
                mid -= mid % step
                break
        else:
            raise ValueError(f"cannot split {slots} into {SLOTS} slots")
        slots.insert(i + 1, Slot(mid, slots[i].soc, slots[i].grid_charge))
    return tuple(slots)


@dataclass
class Candidate:
    tables: dict[date, Table]
    night: int
    expected: Sim
    pessimistic: Sim
    deficit: float          # SOC points the pessimistic case misses, summed over outages
    lowest: float           # lowest pessimistic SOC in an outage
    price: float            # what the candidate is ranked by, UAH

    @property
    def safe(self) -> bool:
        return self.deficit < 0.05


@dataclass
class Plan:
    table: Table                         # what the inverter should hold now
    tables: dict[date, Table]            # every day's own table
    switch_at: Optional[datetime]        # when tomorrow's table takes over
    night: int
    safe: bool
    start: datetime
    end: datetime
    outages: tuple[Outage, ...]          # the ones inside the window
    expected: Sim
    pessimistic: Sim
    price: float = 0.0
    baseline: Optional[Candidate] = None     # the 90/45 table under the same inputs
    kept_previous: bool = False
    calibration: bool = False
    # Every night target tried: (safe, price, lowest pessimistic SOC in an outage).
    candidates: dict[int, tuple[bool, float, float]] = field(default_factory=dict)


class Planner:
    def __init__(self, params: Params):
        self.p = params

    def _fns(self, inputs: Inputs):
        p = self.p

        def hourly(by_hour: Mapping[int, float], factor: float = 1.0):
            return lambda t: by_hour.get(t.hour, 0.0) * factor

        def pv(by_hour: Mapping[tuple[date, int], float]):
            return lambda t: by_hour.get((t.date(), t.hour), 0.0)

        # P80 x1.1, but never below the mean: a few spikes can leave an hour's P80 under it.
        high = {h: max(v, inputs.load_mean.get(h, 0.0)) for h, v in inputs.load_high.items()}
        return (hourly(inputs.load_mean), pv(inputs.pv_p50),
                hourly(high, p.safety_load_factor), pv(inputs.pv_pess))

    def plan(self, inputs: Inputs, previous: Optional[Tables] = None) -> Plan:
        p = self.p
        start, end = window(inputs.now, p)
        outages = tuple((a, b) for a, b in inputs.outages if b > start and a < end)
        if outages:
            end = max(end, max(b for _, b in outages))     # simulate scheduled outages to their end
        days = sorted({local(start).date(), local(end - timedelta(minutes=1)).date()})
        load_mean, pv_p50, load_safe, pv_pess = self._fns(inputs)
        # Outages already under way are what they are; the table can only prepare for the next ones.
        judged = [k for k, (a, _) in enumerate(outages) if a >= start]

        def evaluate(tables: dict[date, Table], night: int) -> Candidate:
            pess = simulate(tables, start, end, inputs.soc, load_safe, pv_pess, outages, p)
            exp = simulate(tables, start, end, inputs.soc, load_mean, pv_p50, outages, p)
            deficit = sum(shortfall(pess, k, p) for k in judged)
            low = min((pess.min_by.get(k, 100.0) for k in judged), default=100.0)
            price = (1 - p.pess_cost_weight) * exp.cost_adj + p.pess_cost_weight * pess.cost_adj
            return Candidate(tables, night, exp, pess, deficit, low, price)

        def better(a: Candidate, b: Optional[Candidate], margin: float) -> bool:
            if b is None:
                return True
            if abs(a.deficit - b.deficit) >= 0.5:
                return a.deficit < b.deficit
            return a.price < b.price - margin

        ctx = (inputs, start, end, outages, judged, load_safe, pv_pess, evaluate, days)
        nights = [p.calibration_soc] if inputs.calibrate else list(range(p.night_min, p.night_max + 1, p.night_step))
        best, tried = None, {}
        for night in nights:
            cand = self._candidate(night, *ctx)
            tried[night] = (cand.safe, cand.price, cand.lowest)
            if better(cand, best, p.hysteresis_uah):
                best = cand
        if not best.safe and not inputs.calibrate and p.calibration_soc > p.night_max:
            cand = self._candidate(p.calibration_soc, *ctx)
            tried[p.calibration_soc] = (cand.safe, cand.price, cand.lowest)
            if better(cand, best, p.hysteresis_uah):
                best = cand

        kept = False
        if previous is not None:
            before = {d: tuple(table_on(previous, d)) for d in days}
            if before != best.tables:
                prev = evaluate(before, before[days[0]][-1].soc)
                if prev.safe >= best.safe and prev.deficit <= best.deficit + 0.5 and \
                        prev.price <= best.price + p.keep_previous_uah:
                    best, kept = prev, True

        baseline = evaluate({d: safe_table(p) for d in days}, 90)
        table, switch_at = table_now(best.tables, inputs.now)
        return Plan(table, best.tables, switch_at, best.night, best.safe, start, end, outages, best.expected,
                    best.pessimistic, best.price, baseline, kept, inputs.calibrate, tried)

    def _candidate(self, night, inputs, start, end, outages, judged, load_safe, pv_pess, evaluate, days) -> Candidate:
        """The night target plus just-in-time charges for the outages it leaves short, a table per day."""
        p = self.p
        charges: dict[int, Charge] = {}
        earliest = _ceil(inputs.now, GRID)

        def build() -> dict[date, list[Slot]]:
            return {d: profile_table(night, list(charges.values()), p, d) for d in days}

        for _ in range(4):
            tables = {d: tuple(v) for d, v in build().items()}
            pess = simulate(tables, start, end, inputs.soc, load_safe, pv_pess, outages, p)
            bad = next((k for k in judged if shortfall(pess, k, p) > 0.05), None)
            if bad is None:
                break
            a, _ = outages[bad]
            short = shortfall(pess, bad, p)
            at_start = simulate(tables, start, a, inputs.soc, load_safe, pv_pess, outages, p).soc_end
            if is_night(a, p) and bad not in charges and at_start >= night - 1:
                break       # the night charge got there in time: only a higher night target helps
            previous = charges.get(bad)
            target = min(100, math.ceil(at_start + short + p.charge_extra_soc))
            if previous is not None:
                target = min(100, max(target, math.ceil(previous.soc + short + p.charge_extra_soc)))
            if target <= at_start + 0.5 or (previous is not None and target <= previous.soc):
                break       # already full when it starts
            hours = (target - at_start) / 100 * p.capacity_kwh / p.charge_efficiency / p.charge_kw \
                + p.charge_lead_min / 60
            begin = _floor(a - timedelta(hours=hours), GRID)
            if previous is not None:
                begin = min(begin, previous.start)
            begin = max(begin, earliest)
            if begin >= a:
                break       # too late to charge before it
            charges[bad] = Charge(begin, a, target)
        points = build()
        for d in days:
            while len(points[d]) > SLOTS:
                points[d] = self._drop_one(points, d, night, evaluate)
        return evaluate({d: pad(v) for d, v in points.items()}, night)

    def _drop_one(self, points: dict[date, list[Slot]], day: date, night: int, evaluate) -> list[Slot]:
        """Too many breakpoints on `day`: drop the one whose loss costs least (the slot before it stretches over)."""
        best, best_key = None, None
        own = points[day]
        for i in range(1, len(own)):
            trial = own[:i] + own[i + 1:]
            merged = [s for j, s in enumerate(trial) if j == 0 or (s.soc, s.grid_charge) != (trial[j - 1].soc, trial[j - 1].grid_charge)]
            cand = evaluate({d: tuple(merged) if d == day else tuple(v) for d, v in points.items()}, night)
            if best is None or (cand.deficit, cand.price) < best_key:
                best, best_key = merged, (cand.deficit, cand.price)
        return best


def _floor(t: datetime, minutes: int) -> datetime:
    lt = local(t)
    return (lt - timedelta(minutes=lt.minute % minutes, seconds=lt.second, microseconds=lt.microsecond)).astimezone(timezone.utc)


def _ceil(t: datetime, minutes: int) -> datetime:
    f = _floor(t, minutes)
    return f if f == t else f + timedelta(minutes=minutes)
