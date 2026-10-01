"""Outage periods (minute ranges within a day) derived from hourly slots."""
from __future__ import annotations

from typing import Optional, Sequence

from app.dtek.models import Slot
from app.timeutil import MINUTES_IN_DAY

Period = tuple[int, int]


def outage_periods(slots: Sequence[Slot]) -> list[Period]:
    """Merge adjacent outage slots into [start, end) minute ranges."""
    periods: list[Period] = []
    for hour, slot in enumerate(slots):
        if not slot.is_outage:
            continue
        begin, finish = slot.off_minutes
        start, end = hour * 60 + begin, hour * 60 + finish
        if periods and periods[-1][1] == start:
            periods[-1] = (periods[-1][0], end)
        else:
            periods.append((start, end))
    return periods


def current_period(slots: Sequence[Slot], minute: int) -> Optional[Period]:
    return next((p for p in outage_periods(slots) if p[0] <= minute < p[1]), None)


def next_period(slots: Sequence[Slot], minute: int) -> Optional[Period]:
    """First period starting after `minute` (the current one, if any, is not "next")."""
    return next((p for p in outage_periods(slots) if p[0] > minute), None)


def upcoming_periods(slots: Sequence[Slot], minute: int) -> list[Period]:
    """Periods still relevant at `minute`: the ongoing one and everything after it."""
    return [p for p in outage_periods(slots) if p[1] > minute]


def is_outage_at(slots: Sequence[Slot], minute: int) -> bool:
    return current_period(slots, minute) is not None


def continues_into(today: Sequence[Slot], tomorrow: Optional[Sequence[Slot]], period: Period) -> Optional[int]:
    """If `period` runs to midnight and tomorrow starts without power, the minute tomorrow it ends."""
    if period[1] != MINUTES_IN_DAY or tomorrow is None:
        return None
    first = outage_periods(tomorrow)
    if first and first[0][0] == 0:
        return first[0][1]
    return None
