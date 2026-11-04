"""The mini app's data (`/api/v1/schedule`) from a snapshot. Served by this service, and by the reserve on
Cloudflare (dtek-brain, edge/) while this service and its mini app are away. Free of I/O.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.dtek.legacy import legacy_row
from app.dtek.models import Snapshot
from app.power.verdict import Actual, current_windows, power_status, schedule_windows
from app.timeutil import KYIV_TZ, format_moment


def scheduled_status(snapshot: Snapshot, now: datetime, lang: str) -> Optional[dict]:
    """Power right now according to today's schedule; None when there is no schedule to go by.

    "maybe" is a house fed by several lines with only some of them off.
    """
    lines = schedule_windows(snapshot, now)
    if lines is None:
        return None
    today = now.astimezone(KYIV_TZ).date()
    current = current_windows(lines, now)
    state = "on" if not current else "off" if len(current) == len(lines) else "maybe"
    starts = [format_moment(w[0], today, lang) for w in current]
    ends = [format_moment(w[1], today, lang) for w in current]
    return {"state": state, "start": " / ".join(dict.fromkeys(starts)), "end": " / ".join(dict.fromkeys(ends))}


def page_payload(snapshot: Optional[Snapshot], now: datetime, source: str, lang: str, healthy: bool,
                 updated_at: Optional[datetime], inverter: Optional[dict] = None,
                 batteries: Optional[list[dict]] = None, actual: Optional[Actual] = None) -> dict:
    payload = {
        # The power really there and DTEK's expectation, for the status headline; None falls back to DTEK's alone.
        "power": power_status(actual, snapshot, now, lang),
        "inverter": inverter,
        "batteries": batteries or [],
        "source": source,
        "parser_type": source,
        "lang": lang,
        "healthy": healthy,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "current_outage": None,
        "today": None,
        "tomorrow": None,
        "outage": None,
        "scheduled_status": None,
        "days": [],
        "schedule_updated": "",
        "outage_updated": "",
        "availability": None,
        "schedules_suspended": False,
    }
    if snapshot is None:
        return payload
    today = now.astimezone(KYIV_TZ).date()
    legacy = legacy_row(snapshot, today)
    payload.update(
        current_outage=legacy["current_outage"],
        today=legacy["today"],
        tomorrow=legacy["tomorrow"],
        outage=snapshot.outage.to_dict() if snapshot.outage else None,
        scheduled_status=scheduled_status(snapshot, now, lang),
        days=[
            {"date": d.date.isoformat(), "label": d.date.strftime("%d.%m.%y"),
             "is_today": d.date == today, "lines": [line.to_dict() for line in d.lines]}
            for d in snapshot.days
            if today <= d.date <= today + timedelta(days=1)
        ],
        schedule_updated=snapshot.schedule_updated,
        outage_updated=snapshot.outage_updated,
        availability=snapshot.availability.value,
        schedules_suspended=snapshot.schedules_suspended,
    )
    return payload
