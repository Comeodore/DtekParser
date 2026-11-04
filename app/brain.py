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

The mini app itself is stood in for as well: `page` gives its data from the reserve's last read, with kem's
Victron as VRM has it (svitlo-edge) for the power really there and the home battery.
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
from app.page import page_payload
from app.power.verdict import Actual
from app.timeutil import KYIV_TZ

# The reserve reads every 5 minutes, and a challenge puts a read off: older than this, the page says the data
# may be out of date.
PAGE_STALE = timedelta(minutes=15)
# The GX logs to VRM every minute. A grid state older than this is not shown as the power there; the battery is
# shown greyed out.
VICTRON_FRESH = timedelta(minutes=10)
BATTERY_FRESH = timedelta(minutes=5)
# As InverterMonitor.STALE_S at home.
DEYE_FRESH = timedelta(minutes=15)


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


def _at(ms: Optional[float]) -> Optional[datetime]:
    return datetime.fromtimestamp(ms / 1000, KYIV_TZ) if ms else None


def _snapshot(request: dict, fetched_at: datetime) -> Snapshot:
    return build_snapshot(
        request["source"],
        parse_house(request["answer"], request["building"]),
        parse_fact(request["fact"]),
        request.get("names") or {},
        fetched_at,
    )


def power_now(victron: Optional[dict], deye: Optional[list], now: datetime) -> tuple[Optional[Actual], list[dict]]:
    """The power really there and the batteries row, from what the worker read for the page.

    victron: svitlo-edge's last VRM read of the home's Victron: grid (true, false or null), grid_since, grid_at,
    soc, battery_w (positive while charging), battery_at; times in ms.
    deye: the building's inverters from Deye Cloud, each name, soc, battery_w, grid (its grid voltage held
    against DEYE_MIN_GRID_VOLTAGE) and at, in DEYE_BATTERIES' order.
    As at home: the Victron's grid first, else the inverters' when every fresh one agrees (no since then)."""
    actual, rows = None, []
    if victron:
        grid_at = _at(victron.get("grid_at"))
        if victron.get("grid") is not None and grid_at is not None and now - grid_at <= VICTRON_FRESH:
            actual = Actual(bool(victron["grid"]), _at(victron.get("grid_since")), "vrm")
        battery_at = _at(victron.get("battery_at"))
        if victron.get("soc") is not None and battery_at is not None:
            rows.append({"name": "home", "soc": victron["soc"], "battery_w": victron.get("battery_w"),
                         "stale": now - battery_at > BATTERY_FRESH})
    grids = set()
    for inverter in deye or []:
        at = _at(inverter.get("at"))
        if at is None:
            continue
        stale = now - at > DEYE_FRESH
        rows.append({"name": inverter["name"], "soc": inverter.get("soc"), "battery_w": inverter.get("battery_w"),
                     "stale": stale})
        if not stale and inverter.get("grid") is not None:
            grids.add(bool(inverter["grid"]))
    if actual is None and len(grids) == 1:
        actual = Actual(grids.pop(), None, "deye_batteries")
    return actual, rows


def page(request: dict) -> dict:
    """The mini app's data while the home service's own is down. request: source, now (ms), building, answer,
    fact, names and read_at (ms) of the reserve's last good read, lang, and for kem `victron` and `deye` (see
    power_now)."""
    now = _at(request["now"])
    read_at = _at(request["read_at"])
    actual, batteries = power_now(request.get("victron"), request.get("deye"), now)
    payload = page_payload(_snapshot(request, read_at), now, request["source"], request["lang"],
                           now - read_at < PAGE_STALE, read_at, batteries=batteries, actual=actual)
    payload["reserve"] = True
    return payload


def process(request: dict) -> dict:
    """request: source, now (ms), building, answer (getHomeNum), fact, names, and from the home service's last
    push: baseline, fingerprint, lang, notify_outages. With `shown`, the days the chats were last shown (as the
    reply's `shown` of the call before; [] for none), the schedules are sent as well, and the reply has the
    next `shown`. With `scheduled_notices`, the notices of outages by the schedule as well."""
    now = _at(request["now"])
    snapshot = _snapshot(request, now)
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
