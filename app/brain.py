"""What the reserve on Cloudflare runs as dtek-brain (cf-watch/brain.py): one read of DTEK in, the next
baseline and the chats' texts out, decided exactly as a cycle of this service decides them.

The reserve's Durable Object reads DTEK and keeps the baseline; this module only parses, detects and renders.
It is bundled with the modules it imports, so nothing here may need the browser, the database or Telegram.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.dtek.protocol import build_snapshot, parse_fact, parse_house
from app.engine.detector import Baseline, detect
from app.engine.events import Event, ScheduleEvent
from app.notify.router import event_texts
from app.timeutil import KYIV_TZ


def describe(event: Event) -> str:
    if isinstance(event, ScheduleEvent):
        return f"schedule {event.day.isoformat()} {event.change.value}"
    return f"outage {event.change.value}"


def process(request: dict) -> dict:
    """request: source, now (ms), building, answer (getHomeNum), fact, names, and from the home service's last
    push: baseline, fingerprint, lang, notify_outages."""
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
    return {
        "baseline": detection.baseline.to_dict(),
        "texts": event_texts(source, detection.events, snapshot, now),
        "events": [describe(e) for e in detection.events],
        "initialized": detection.initialized,
    }
