"""The mini app's status: the power really there, and what DTEK expects at the moment.

DTEK is often off by a while: the power is back before the scheduled end, or goes off outside the schedule.
The source's own sensor says what is, DTEK's notice or schedule what should be; the page shows the first as
the headline and the second under it, with how they differ. Free of I/O, like the state machine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

from app.dtek.models import Outage, OutageKind, Snapshot
from app.engine.periods import continues_into, outage_periods
from app.timeutil import KYIV_TZ, MINUTES_IN_DAY, format_moment

Window = tuple[datetime, datetime]


@dataclass(frozen=True)
class Actual:
    """Power at the address right now, by a sensor there."""
    on: bool
    since: Optional[datetime]   # None when the source keeps no history
    source: str                 # "home_assistant" or "deye_batteries" (the building's inverters)


@dataclass(frozen=True)
class Expected:
    state: str                  # "off", "maybe" (only some of the house's lines are off) or "on"
    kind: str                   # "schedule", or a notice the schedule does not show: "emergency", "planned", "dtek"
    start: Optional[datetime] = None
    end: Optional[datetime] = None


def schedule_windows(snapshot: Snapshot, now: datetime) -> Optional[list[list[Window]]]:
    """Today's scheduled outages per line as moments, one running to midnight ending when tomorrow's first does;
    None when there is no schedule to go by."""
    today = now.astimezone(KYIV_TZ).date()
    day = snapshot.day(today)
    if snapshot.schedules_suspended or day is None or not day.lines:
        return None
    midnight = datetime.combine(today, time(), KYIV_TZ)
    tomorrow = snapshot.day(today + timedelta(days=1))
    lines = []
    for line in day.lines:
        following = next((l for l in tomorrow.lines if l.group == line.group), None) if tomorrow else None
        windows = []
        for period in outage_periods(line.slots):
            tail = continues_into(line.slots, following.slots if following else None, period)
            end = period[1] if tail is None else MINUTES_IN_DAY + tail
            windows.append((midnight + timedelta(minutes=period[0]), midnight + timedelta(minutes=end)))
        lines.append(windows)
    return lines


def current_windows(lines: list[list[Window]], now: datetime) -> list[Window]:
    """The scheduled outage each line is in at `now`; lines with power are left out."""
    found = []
    for windows in lines:
        current = next((w for w in windows if w[0] <= now < w[1]), None)
        if current is not None:
            found.append(current)
    return found


def _in_force(outage: Optional[Outage], now: datetime) -> bool:
    """DTEK's notice for the present: not one announced ahead, nor one whose restoration time has passed."""
    if outage is None:
        return False
    return (outage.start_at is None or outage.start_at <= now) and (outage.end_at is None or now < outage.end_at)


def expected_now(snapshot: Snapshot, lines: Optional[list[list[Window]]], now: datetime) -> Optional[Expected]:
    """DTEK's notice while it is in force, else the schedule; None when DTEK has neither for now."""
    scheduled = None
    if lines is not None:
        current = current_windows(lines, now)
        if not current:
            scheduled = Expected("on", "schedule")
        else:
            state = "off" if len(current) == len(lines) else "maybe"
            scheduled = Expected(state, "schedule", min(w[0] for w in current), max(w[1] for w in current))
    outage = snapshot.outage
    if _in_force(outage, now):
        kind = {OutageKind.EMERGENCY: "emergency", OutageKind.PLANNED: "planned"}.get(outage.kind)
        if kind is None:
            kind = "schedule" if scheduled is not None and scheduled.state != "on" else "dtek"
        return Expected("off", kind, outage.start_at, outage.end_at)
    return scheduled


def judge(actual: Actual, expected: Expected, snapshot: Snapshot, lines: Optional[list[list[Window]]],
          now: datetime) -> tuple[str, Optional[datetime]]:
    """How the real power stands against DTEK's expectation, and for "late" when it was due back.

    "agree"; on while DTEK has an outage: "early" (back before its end) or "not_yet" (not gone off since it
    began); off while DTEK has power: "late" (an outage that began earlier and should have ended) or
    "unscheduled"; "differs" when the source cannot tell since when.
    """
    if expected.state == "maybe" or actual.on == (expected.state == "on"):
        return "agree", None
    if actual.since is None:
        return "differs", None
    if actual.on:
        if expected.start is None:
            return "differs", None
        return ("early" if actual.since >= expected.start else "not_yet"), None
    # Off with DTEK saying on: is it an outage, scheduled or notified, that is running past its end?
    ended = [w for windows in (lines or []) for w in windows if actual.since < w[1] <= now]
    outage = snapshot.outage
    if outage is not None and outage.end_at is not None and actual.since < outage.end_at <= now:
        ended.append((outage.start_at or outage.end_at, outage.end_at))
    if ended:
        return "late", max(w[1] for w in ended)
    return "unscheduled", None


def power_status(actual: Optional[Actual], snapshot: Optional[Snapshot], now: datetime, lang: str) -> Optional[dict]:
    """The mini app's `power`; None when no sensor knows, and the page shows DTEK's status alone as before."""
    if actual is None:
        return None
    today = now.astimezone(KYIV_TZ).date()

    def moment(at: Optional[datetime]) -> Optional[str]:
        return format_moment(at, today, lang) if at is not None else None

    lines = schedule_windows(snapshot, now) if snapshot is not None else None
    expected = expected_now(snapshot, lines, now) if snapshot is not None else None
    verdict, due = judge(actual, expected, snapshot, lines, now) if expected is not None else (None, None)
    return {
        "state": "on" if actual.on else "off",
        "since": moment(actual.since),
        "since_at": actual.since.isoformat() if actual.since else None,
        "source": actual.source,
        "expected": None if expected is None else {
            "state": expected.state,
            "kind": expected.kind,
            "until": moment(expected.end) if expected.state != "on" else None,
        },
        "verdict": verdict,
        "due": moment(due),
    }
