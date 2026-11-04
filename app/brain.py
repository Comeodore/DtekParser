"""What the reserve on Cloudflare runs as dtek-brain (edge/brain.py): one read of DTEK in, the next
baseline and the chats' texts out, decided exactly as a cycle of this service decides them.

The reserve's Durable Object reads DTEK and keeps the baseline; this module only parses, detects and renders.
It is bundled with the modules it imports, so nothing here may need the browser, the database or Telegram.

One thing the reserve sends that this service does not: the schedules themselves. The mini app shows them while
this service runs, but it is down with it, so while the reserve is in charge the chats get today's and
tomorrow's schedule as text each time they are first seen or come to differ from what the chats were last
shown, even when there is no change to report: on taking a source over, when schedules apply again after an
emergency without them, when tomorrow is published with no outages.

For a source the worker marks so (kem), DTEK's notices of outages by the schedule are sent too: the start, a new
end and the end. This service leaves them to the mini app, which is down with it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Iterable, Optional

from app.dtek.models import DaySchedule, Snapshot
from app.dtek.protocol import build_snapshot, parse_fact, parse_house
from app.engine.detector import Baseline, detect
from app.engine.events import Event, ScheduleEvent
from app.notify.messages import render_days
from app.notify.router import event_texts
from app.timeutil import KYIV_TZ


def describe(event: Event) -> str:
    if isinstance(event, ScheduleEvent):
        return f"schedule {event.day.isoformat()} {event.change.value}"
    return f"outage {event.change.value}"


def to_show(shown: dict[date, DaySchedule], events: Iterable[Event], snapshot: Snapshot,
            now: datetime) -> tuple[dict[date, DaySchedule], list[DaySchedule]]:
    """What the chats will have seen of today and tomorrow, and the days among them to send as they are.

    A day the events already report needs nothing more: its message shows it as it is now. Hours gone by do
    not count, as in the detector. While DTEK says schedules do not apply, none is sent."""
    local_now = now.astimezone(KYIV_TZ)
    today = local_now.date()
    reported = {e.day for e in events if isinstance(e, ScheduleEvent)}
    seen: dict[date, DaySchedule] = {}
    due: list[DaySchedule] = []
    for day in (today, today + timedelta(days=1)):
        previous, current = shown.get(day), snapshot.day(day)
        if day in reported:
            if current is not None:
                seen[day] = current
            continue
        if current is None or snapshot.schedules_suspended:
            if previous is not None:
                seen[day] = previous
            continue
        from_hour = local_now.hour if day == today else 0
        if previous is None or previous.view(from_hour) != current.view(from_hour):
            due.append(current)
        seen[day] = current
    return seen, due


def process(request: dict) -> dict:
    """request: source, now (ms), building, answer (getHomeNum), fact, names, and from the home service's last
    push: baseline, fingerprint, lang, notify_outages. With `shown`, the days the chats were last shown (as the
    reply's `shown` of the call before; [] for none), the schedules are sent as well, and the reply has the
    next `shown`. With `scheduled_notices`, the notices of outages by the schedule as well."""
    now = datetime.fromtimestamp(request["now"] / 1000, KYIV_TZ)
    snapshot = build_snapshot(
        request["source"],
        parse_house(request["answer"], request["building"]),
        parse_fact(request["fact"]),
        request.get("names") or {},
        now,
    )
    detection = detect(Baseline.from_dict(request.get("baseline")), snapshot, now, request["fingerprint"])
    source = SimpleNamespace(name=request["source"], lang=request["lang"], notify_outages=request["notify_outages"])
    reply = {
        "baseline": detection.baseline.to_dict(),
        "texts": event_texts(source, detection.events, snapshot, now, bool(request.get("scheduled_notices"))),
        "events": [describe(e) for e in detection.events],
        "initialized": detection.initialized,
    }
    shown: Optional[list] = request.get("shown")
    if shown is not None:
        seen, due = to_show({d.date: d for d in map(DaySchedule.from_dict, shown)}, detection.events, snapshot, now)
        if due:
            reply["texts"].append(render_days(due, snapshot, now, request["lang"]))
        reply["shown"] = [d.to_dict() for d in seen.values()]
    return reply
