"""The JSON shapes the old parser wrote to dtek_schedule and served from the API.

Kept so the mini app and a rollback to the old containers keep working.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from app.dtek.models import DaySchedule, Outage, OutageKind, Snapshot
from app.timeutil import KYIV_TZ

LEGACY_STATUS = {OutageKind.EMERGENCY: "emergency", OutageKind.PLANNED: "scheduled", OutageKind.OTHER: "scheduled"}


def legacy_outage(outage: Optional[Outage], updated: str) -> dict:
    if outage is None:
        return {"status": "power_on", "reason": "", "start_time": "", "restoration_time": "", "last_updated": updated}
    return {
        "status": LEGACY_STATUS[outage.kind],
        "reason": outage.reason,
        "start_time": outage.start,
        "restoration_time": outage.end,
        "last_updated": updated,
    }


def legacy_day(day: Optional[DaySchedule], updated: str) -> Optional[dict]:
    """First line only, as the old parser read the first table on the page."""
    if day is None or not day.lines:
        return None
    return {
        "date": day.date.strftime("%d.%m.%y"),
        "slots": [{"hour": h, "status": s.value} for h, s in enumerate(day.lines[0].slots)],
        "last_updated": updated,
    }


def legacy_row(snapshot: Snapshot, today: date) -> dict:
    return {
        "current_outage": legacy_outage(snapshot.outage, snapshot.outage_updated),
        "today": legacy_day(snapshot.day(today), snapshot.schedule_updated),
        "tomorrow": legacy_day(snapshot.day(today + timedelta(days=1)), snapshot.schedule_updated),
        "updated_at": snapshot.fetched_at.astimezone(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }
