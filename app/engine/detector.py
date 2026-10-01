"""Change detection between the last accepted state (baseline) and a fresh snapshot.

Design rules, each one fixing a failure mode the old bots had:

* Days are matched by their calendar date, not by "today"/"tomorrow" slots, so
  midnight needs no special casing: yesterday's "tomorrow" simply is today.
* The baseline is persisted by the caller in the same transaction as the
  notifications, so a restart neither re-sends nor swallows anything.
* A failed fetch never reaches this module, so "DTEK did not answer" can no
  longer look like "DTEK published nothing".
* Negative changes (a day vanishing, an outage notice disappearing) must be
  observed several times in a row before they count, so one odd answer cannot
  produce a false "cancelled" followed by a "published" a minute later.
* For today only the remaining hours are compared: DTEK touching hours that
  are already over is not news.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from app.dtek.models import DaySchedule, Outage, Snapshot
from app.engine.events import Event, OutageChange, OutageEvent, ScheduleChange, ScheduleEvent
from app.timeutil import KYIV_TZ

ABSENT_CONFIRMATIONS = 3
CLEAR_CONFIRMATIONS = 2
BASELINE_VERSION = 1


@dataclass
class Baseline:
    fingerprint: str
    days: dict[date, DaySchedule] = field(default_factory=dict)
    outage: Optional[Outage] = None
    absent: dict[date, int] = field(default_factory=dict)
    clear_streak: int = 0

    def to_dict(self) -> dict:
        return {
            "version": BASELINE_VERSION,
            "fingerprint": self.fingerprint,
            "days": [d.to_dict() for d in self.days.values()],
            "outage": self.outage.to_dict() if self.outage else None,
            "absent": {d.isoformat(): n for d, n in self.absent.items()},
            "clear_streak": self.clear_streak,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["Baseline"]:
        if not data or data.get("version") != BASELINE_VERSION:
            return None
        days = (DaySchedule.from_dict(d) for d in data.get("days", ()))
        return cls(
            fingerprint=data["fingerprint"],
            days={d.date: d for d in days},
            outage=Outage.from_dict(data.get("outage")),
            absent={date.fromisoformat(k): int(v) for k, v in data.get("absent", {}).items()},
            clear_streak=int(data.get("clear_streak", 0)),
        )


@dataclass(frozen=True)
class Detection:
    events: list[Event]
    baseline: Baseline
    initialized: bool = False


def detect(baseline: Optional[Baseline], snapshot: Snapshot, now: datetime, fingerprint: str) -> Detection:
    local_now = now.astimezone(KYIV_TZ)
    today = local_now.date()
    watched = (today, today + timedelta(days=1))

    if baseline is None or baseline.fingerprint != fingerprint:
        # First run, or the address changed: there is nothing meaningful to diff against.
        initial = Baseline(
            fingerprint=fingerprint,
            days={d: s for d in watched if (s := snapshot.day(d)) is not None},
            outage=snapshot.outage,
        )
        return Detection([], initial, initialized=True)

    events: list[Event] = []
    suspended = snapshot.schedules_suspended
    days: dict[date, DaySchedule] = {}
    absent: dict[date, int] = {}

    for day in watched:
        previous = baseline.days.get(day)
        current = snapshot.day(day)
        from_hour = local_now.hour if day == today else 0

        if current is None:
            if previous is None:
                continue
            streak = baseline.absent.get(day, 0) + 1
            if streak < ABSENT_CONFIRMATIONS:
                days[day] = previous
                absent[day] = streak
                continue
            if not suspended and previous.has_outages(from_hour):
                events.append(ScheduleEvent(day, ScheduleChange.CLEARED, None, previous))
            continue

        days[day] = current
        if suspended:
            # Schedules don't apply right now; keep tracking silently so the
            # baseline is current once they do again.
            continue
        change = _classify(previous, current, from_hour)
        if change is not None:
            events.append(ScheduleEvent(day, change, current, previous))

    outage, clear_streak, outage_events = _detect_outage(baseline, snapshot.outage)
    events.extend(outage_events)
    return Detection(events, Baseline(fingerprint, days, outage, absent, clear_streak))


def _classify(previous: Optional[DaySchedule], current: DaySchedule, from_hour: int) -> Optional[ScheduleChange]:
    if previous is not None and previous.view(from_hour) == current.view(from_hour):
        return None
    had = previous is not None and previous.has_outages(from_hour)
    has = current.has_outages(from_hour)
    if has and not had:
        return ScheduleChange.PUBLISHED
    if had and not has:
        return ScheduleChange.CLEARED
    if had and has:
        return ScheduleChange.CHANGED
    return None


def _detect_outage(baseline: Baseline, current: Optional[Outage]) -> tuple[Optional[Outage], int, list[Event]]:
    previous = baseline.outage
    if current is None:
        if previous is None:
            return None, 0, []
        streak = baseline.clear_streak + 1
        if streak < CLEAR_CONFIRMATIONS:
            return previous, streak, []
        return None, 0, [OutageEvent(OutageChange.ENDED, None, previous)]

    if previous is None or _is_another_outage(previous, current):
        return current, 0, [OutageEvent(OutageChange.STARTED, current, previous)]
    if (current.kind, current.reason, current.schedules_suspended) != (
        previous.kind, previous.reason, previous.schedules_suspended
    ):
        return current, 0, [OutageEvent(OutageChange.UPDATED, current, previous)]
    if current.end != previous.end:
        return current, 0, [OutageEvent(OutageChange.RESTORATION_CHANGED, current, previous)]
    return current, 0, []


def _is_another_outage(previous: Outage, current: Outage) -> bool:
    return bool(previous.start and current.start and previous.start != current.start)
