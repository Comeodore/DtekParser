"""Turns what dtek-*.com.ua returns into a Snapshot.

Two payloads matter, both produced by the site's own discon-schedule.js:

* ``DisconSchedule.fact`` — embedded in the page and refreshed through the AJAX
  call below: ``{"update": "01.10.2026 06:50", "today": <unix>, "data":
  {"<unix day start>": {"GPV4.1": {"1": "yes", ..., "24": "no"}}}}``.
  Hour key "1" is 00-01, "24" is 23-24.
* ``POST /ua/ajax method=getHomeNum`` — every house of the street with its
  groups (``sub_type_reason``) and the current outage notice
  (``type``/``sub_type``/``start_date``/``end_date``).

Parsing is strict on purpose: an unexpected shape raises ProtocolError, so a
change on DTEK's side makes the source fail loudly (missed heartbeat) instead of
quietly producing a wrong schedule.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional

from app.dtek.models import (
    HOURS_IN_DAY,
    Availability,
    DaySchedule,
    Line,
    Outage,
    OutageKind,
    Slot,
    Snapshot,
)
from app.timeutil import KYIV_TZ

logger = logging.getLogger(__name__)

# Values come from DisconSchedule.preset.time_type; the "m*" ("maybe") ones only
# show up in the weekly plan, but the site renders them like the definite ones.
SLOT_VALUES = {
    "yes": Slot.ON,
    "no": Slot.OFF,
    "maybe": Slot.OFF,
    "first": Slot.FIRST_HALF_OFF,
    "mfirst": Slot.FIRST_HALF_OFF,
    "second": Slot.SECOND_HALF_OFF,
    "msecond": Slot.SECOND_HALF_OFF,
}
KNOWN_TIME_TYPES = frozenset(SLOT_VALUES)

# The site hides the tables for this sub_type and warns that schedules don't apply.
SUSPENDED_MARKER = "без застосування графіку"
EMERGENCY_MARKERS = ("аварійн", "екстрен")
PLANNED_TYPE = "1"
# discon-schedule.js renders at most two lines per house.
MAX_LINES = 2


class ProtocolError(Exception):
    """DTEK answered with something we do not understand."""


@dataclass(frozen=True)
class Fact:
    update: str
    days: dict[date, dict[str, tuple[Slot, ...]]]


@dataclass(frozen=True)
class House:
    groups: tuple[str, ...]
    outage: Optional[Outage]
    outage_updated: str
    availability: Availability


def parse_fact(raw: Any) -> Fact:
    if not isinstance(raw, Mapping):
        raise ProtocolError(f"fact is {type(raw).__name__}, expected an object")
    update = raw.get("update")
    if not isinstance(update, str):
        raise ProtocolError("fact.update is missing")
    data = raw.get("data")
    # PHP serialises an empty map as [], the site checks `fact.data.length == 0`.
    if data == [] or data is None:
        return Fact(update, {})
    if not isinstance(data, Mapping):
        raise ProtocolError(f"fact.data is {type(data).__name__}")

    days: dict[date, dict[str, tuple[Slot, ...]]] = {}
    for key, groups in data.items():
        day = _day_from_key(key)
        if not isinstance(groups, Mapping):
            raise ProtocolError(f"fact.data[{key}] is {type(groups).__name__}")
        days[day] = {group: _parse_hours(group, hours) for group, hours in groups.items()}
    return Fact(update, days)


def _day_from_key(key: str) -> date:
    try:
        moment = datetime.fromtimestamp(int(key), KYIV_TZ)
    except (TypeError, ValueError, OverflowError) as e:
        raise ProtocolError(f"fact day key {key!r} is not a timestamp") from e
    if (moment.hour, moment.minute, moment.second) != (0, 0, 0):
        raise ProtocolError(f"fact day key {key!r} is not a Kyiv midnight ({moment.isoformat()})")
    return moment.date()


def _parse_hours(group: str, hours: Any) -> tuple[Slot, ...]:
    if not isinstance(hours, Mapping):
        raise ProtocolError(f"{group}: hours are {type(hours).__name__}")
    expected = {str(h) for h in range(1, HOURS_IN_DAY + 1)}
    if set(hours) != expected:
        raise ProtocolError(f"{group}: unexpected hour keys {sorted(hours)}")
    try:
        return tuple(SLOT_VALUES[hours[str(h)]] for h in range(1, HOURS_IN_DAY + 1))
    except KeyError as e:
        raise ProtocolError(f"{group}: unknown slot value {e.args[0]!r}") from e


def parse_house(answer: Any, building: str) -> House:
    if not isinstance(answer, Mapping):
        raise ProtocolError(f"getHomeNum answer is {type(answer).__name__}")
    if answer.get("result") is not True:
        raise ProtocolError(f"getHomeNum result={answer.get('result')!r}")
    houses = answer.get("data")
    if not isinstance(houses, Mapping):
        raise ProtocolError(f"getHomeNum data is {type(houses).__name__}")
    entry = houses.get(building)
    if not isinstance(entry, Mapping):
        sample = ", ".join(list(houses)[:15])
        raise ProtocolError(f"house {building!r} is not in the answer (has: {sample}…)")

    groups = tuple(g for g in (entry.get("sub_type_reason") or ()) if isinstance(g, str) and g)
    if entry.get("cek"):
        availability = Availability.OTHER_OPERATOR
    elif entry.get("voluntarily"):
        availability = Availability.BUILDING_MANAGED
    elif not groups:
        availability = Availability.NO_GROUP
    else:
        availability = Availability.OK

    return House(
        groups=groups,
        outage=_parse_outage(entry),
        outage_updated=str(answer.get("updateTimestamp") or ""),
        availability=availability,
    )


def _parse_outage(entry: Mapping) -> Optional[Outage]:
    sub_type = str(entry.get("sub_type") or "").strip()
    start = str(entry.get("start_date") or "").strip()
    end = str(entry.get("end_date") or "").strip()
    kind_code = str(entry.get("type") or "").strip()
    # Same condition the site uses to decide between "no blackout" and the outage notice.
    if not (sub_type or start or end):
        return None

    lowered = sub_type.lower()
    if kind_code == PLANNED_TYPE:
        kind = OutageKind.PLANNED
    elif any(marker in lowered for marker in EMERGENCY_MARKERS):
        kind = OutageKind.EMERGENCY
    else:
        kind = OutageKind.OTHER
    return Outage(
        kind=kind,
        reason=sub_type,
        start=start,
        end=end,
        schedules_suspended=SUSPENDED_MARKER in lowered,
    )


def build_snapshot(
    source: str,
    house: House,
    fact: Fact,
    names: Mapping[str, str],
    fetched_at: datetime,
) -> Snapshot:
    groups = _schedule_groups(house.groups, fact, names)
    days: list[DaySchedule] = []
    if house.availability is Availability.OK:
        for day in sorted(fact.days):
            lines = tuple(
                Line(group, names.get(group, group), fact.days[day][group])
                for group in groups
                if group in fact.days[day]
            )
            if lines:
                days.append(DaySchedule(day, lines))

    return Snapshot(
        source=source,
        fetched_at=fetched_at,
        groups=house.groups,
        days=tuple(days),
        outage=house.outage,
        outage_updated=house.outage_updated,
        schedule_updated=fact.update,
        availability=house.availability,
    )


_reported_reductions: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()


def _schedule_groups(groups: tuple[str, ...], fact: Fact, names: Mapping[str, str]) -> tuple[str, ...]:
    """Mirror tableRender(): keep the first two groups the site knows and has data for."""
    candidates = groups[:MAX_LINES]
    kept = tuple(
        g for g in candidates
        if (not names or g in names) and all(g in day for day in fact.days.values())
    )
    if kept != candidates and (candidates, kept) not in _reported_reductions:
        _reported_reductions.add((candidates, kept))
        logger.warning(f"Groups {candidates} reduced to {kept}: missing from the site's schedule data")
    return kept


def unknown_time_types(time_types: Any) -> list[str]:
    """Slot values the site declares but we cannot map yet (for a one-off warning)."""
    if not isinstance(time_types, (list, tuple)):
        return []
    return sorted(t for t in time_types if t not in KNOWN_TIME_TYPES)
