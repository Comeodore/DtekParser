"""What the reserve on Cloudflare runs as dtek-brain (edge/brain.py): one read of DTEK in, the next
baseline and the chats' texts out, decided exactly as a cycle of this service decides them.

The reserve's Durable Object reads DTEK and keeps the baseline; this module only parses, detects and renders.
It is bundled with the modules it imports, so nothing here may need the browser, the database or Telegram.

One thing the reserve sends that this service does not: the schedules themselves. The mini app shows them while
this service runs, but it is down with it, so while the reserve is in charge the chats get today's and
tomorrow's schedule as text each time they are first seen or come to differ from what the chats were last
shown, even when there is no change to report: on taking a source over, when schedules apply again after an
emergency without them, when tomorrow is published with no outages.

While this service is away the reserve also says when the power is gone or back (`power`): kem's from its Victron
as VRM has it, krem's from its Deye inverter, judged and worded as this service does with Home Assistant and the
inverter, going on from the state this service last pushed.

The mini app itself is stood in for as well: `page` gives its data from the reserve's last read, with kem's
Victron as VRM has it for the power really there and the home battery, the building's Deye inverters in kem's
batteries row and krem's inverter diagram, from Deye Cloud.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Iterable, Optional

from app.dtek.models import DaySchedule, Snapshot
from app.dtek.protocol import build_snapshot, parse_fact, parse_house
from app.engine.detector import Baseline, detect
from app.engine.events import Event, ScheduleEvent
from app.inverter.reading import STALE_S, DeyeError, InverterReading, parse_device_latest
from app.notify.messages import render_days, render_power
from app.notify.router import event_texts
from app.page import page_payload
from app.power.state import PowerState, PowerStatus, next_state
from app.power.verdict import Actual
from app.timeutil import KYIV_TZ

# The reserve reads every 5 minutes, and a challenge puts a read off: older than this, the page says the data
# may be out of date.
PAGE_STALE = timedelta(minutes=15)
# The GX logs to VRM every minute. A grid state older than this is not shown as the power there; the battery is
# shown greyed out.
VICTRON_FRESH = timedelta(minutes=10)
BATTERY_FRESH = timedelta(minutes=5)
# As DEYE_MIN_GRID_VOLTAGE at home.
MIN_GRID_V = 100.0


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


def deye_readings(deye: Optional[dict]) -> dict[str, InverterReading]:
    """The inverters in device/latest's deviceDataList by serial, parsed as at home; one without data is left out."""
    readings = {}
    for item in (deye or {}).get("items") or []:
        try:
            readings[item.get("deviceSn")] = parse_device_latest(item, deye.get("min_grid_voltage", MIN_GRID_V))
        except DeyeError:
            continue
    return readings


def power_now(victron: Optional[dict], deye: Optional[dict], now: datetime) -> tuple[Optional[Actual], list[dict]]:
    """The power really there and the batteries row, from what the worker read for the page.

    victron: the worker's last VRM read of the home's Victron: grid (true, false or null), grid_since, grid_at,
    soc, battery_w (positive while charging), battery_at; times in ms.
    deye: the inverters from Deye Cloud: `items` (device/latest's deviceDataList), `batteries` ([name, serial] in
    DEYE_BATTERIES' order) and `min_grid_voltage`.
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
    readings = deye_readings(deye)
    grids = set()
    for name, sn in (deye or {}).get("batteries") or []:
        reading = readings.get(sn)
        if reading is None:
            continue
        stale = (now - reading.at).total_seconds() > STALE_S
        rows.append({"name": name, "soc": reading.soc, "battery_w": reading.battery_w, "stale": stale})
        if not stale:
            grids.add(reading.grid_present)
    if actual is None and len(grids) == 1:
        actual = Actual(grids.pop(), None, "deye_batteries")
    return actual, rows


def inverter_now(deye: Optional[dict], now: datetime) -> Optional[dict]:
    """The inverter diagram's data (krem), as InverterMonitor.payload at home, from `deye`'s `main` serial. What
    needs the home service is left out: today's grid by tariff (the page shows the total), the sun forecast
    (deye-reserve) and until when the battery lasts."""
    reading = deye_readings(deye).get((deye or {}).get("main"))
    if reading is None:
        return None
    body = reading.to_dict(now, STALE_S)
    body.update(grid_night_kwh=None, grid_day_kwh=None, pv_forecast_kwh=None, battery_until=None)
    return body


def page(request: dict) -> dict:
    """The mini app's data while the home service's own is down. request: source, now (ms), building, answer,
    fact, names and read_at (ms) of the reserve's last good read, lang, `victron` (kem) and `deye`, whose `main`
    is the inverter of the diagram (krem) and `batteries` the batteries row's (kem); see power_now."""
    now = _at(request["now"])
    read_at = _at(request["read_at"])
    actual, batteries = power_now(request.get("victron"), request.get("deye"), now)
    payload = page_payload(_snapshot(request, read_at), now, request["source"], request["lang"],
                           now - read_at < PAGE_STALE, read_at, inverter_now(request.get("deye"), now), batteries,
                           actual)
    payload["reserve"] = True
    return payload


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _context(request: dict) -> Optional[Snapshot]:
    """The schedule a power message is worded with: the reserve's last read of DTEK (building, answer, fact, names,
    read_at), else the home service's last pushed `baseline` (its days and DTEK's notice, as of read_at), else
    none."""
    if request.get("answer"):
        return _snapshot(request, _at(request["read_at"]))
    baseline = Baseline.from_dict(request.get("baseline"))
    if baseline is None:
        return None
    return Snapshot(request["source"], _at(request.get("read_at") or request["now"]), (),
                    tuple(baseline.days.values()), baseline.outage, "", "")


def power(request: dict) -> dict:
    """The power at a source's address, observed by the worker: request: source, now (ms), lang, `state` (the
    reply's `state` of the call before, or the home service's last; null at first), `observed` (on, at: when the
    reading that shows it was taken, ms; battery: "71%" or "N/A") and the schedule's context (see _context).
    Returns the next `state` and the message, if the power changed.

    As at home: a first observation only sets the state; a reading is judged at its own time, so a minute-old one
    is no late notice."""
    now = _at(request["now"])
    stored = request.get("state")
    previous = PowerState(PowerStatus.ON if stored["on"] else PowerStatus.OFF, _at(stored["since"])) if stored else None
    observed = request["observed"]
    at = _at(observed["at"])
    state, transition = next_state(previous, PowerStatus.ON if observed["on"] else PowerStatus.OFF, at, at,
                                   observed.get("battery") or "N/A", from_snapshot=False)
    texts = []
    if transition is not None:
        texts.append(render_power(transition, _context(request), now, request["lang"]))
    return {"state": {"on": state.status is PowerStatus.ON, "since": _ms(state.since)}, "texts": texts}


def process(request: dict) -> dict:
    """request: source, now (ms), building, answer (getHomeNum), fact, names, and from the home service's last
    push: baseline, fingerprint, lang, notify_outages. With `shown`, the days the chats were last shown (as the
    reply's `shown` of the call before; [] for none), the schedules are sent as well, and the reply has the
    next `shown`."""
    now = _at(request["now"])
    snapshot = _snapshot(request, now)
    detection = detect(Baseline.from_dict(request.get("baseline")), snapshot, now, request["fingerprint"])
    source = SimpleNamespace(name=request["source"], lang=request["lang"], notify_outages=request["notify_outages"])
    reply = {
        "baseline": detection.baseline.to_dict(),
        "texts": event_texts(source, detection.events, snapshot, now),
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
