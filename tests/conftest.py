import json
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import pytest

from app.config import load_settings
from app.dtek.models import DaySchedule, Line, Outage, OutageKind, Slot, Snapshot
from app.timeutil import KYIV_TZ

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def kyiv(*args) -> datetime:
    return datetime(*args, tzinfo=KYIV_TZ)


def slots(off: Iterable[int] = (), first: Iterable[int] = (), second: Iterable[int] = ()) -> tuple[Slot, ...]:
    result = [Slot.ON] * 24
    for h in off:
        result[h] = Slot.OFF
    for h in first:
        result[h] = Slot.FIRST_HALF_OFF
    for h in second:
        result[h] = Slot.SECOND_HALF_OFF
    return tuple(result)


def day(d: date, *lines: tuple[str, tuple[Slot, ...]]) -> DaySchedule:
    return DaySchedule(d, tuple(Line(group, f"Черга {group[3:]}", s) for group, s in lines))


def one(d: date, off: Iterable[int] = (), group: str = "GPV4.1", **kw) -> DaySchedule:
    return day(d, (group, slots(off, **kw)))


def snapshot(days: Iterable[DaySchedule] = (), outage: Optional[Outage] = None,
             fetched: Optional[datetime] = None, source: str = "kem") -> Snapshot:
    return Snapshot(
        source=source,
        fetched_at=fetched or kyiv(2026, 10, 1, 12, 0),
        groups=("GPV4.1",),
        days=tuple(sorted(days, key=lambda d: d.date)),
        outage=outage,
        outage_updated="12:00 01.10.2026",
        schedule_updated="01.10.2026 06:50",
    )


def emergency(end: str = "15:04 01.10.2026", start: str = "11:04 01.10.2026",
              reason: str = "Аварійні ремонтні роботи", suspended: bool = False) -> Outage:
    return Outage(OutageKind.EMERGENCY, reason, start, end, suspended)


BASE_ENV = {
    "DATABASE_URL": "postgresql://user:pass@localhost/db",
    "SOURCES": "kem,krem",
    "KEM_STREET": "просп. Європейського Союзу",
    "KEM_BUILDING": "43",
    "KEM_PORT": "9999",
    "KEM_LANG": "en",
    "KEM_BOT": "lightbot",
    "KEM_CHAT_IDS": "111,222",
    "KREM_STREET": "вул. Садова",
    "KREM_BUILDING": "10",
    "KREM_SETTLEMENT": "с. Круглик",
    "KREM_PORT": "9998",
    "KREM_LANG": "uk",
    "KREM_BOT": "schedulebot",
    "KREM_CHAT_IDS": "222,333",
    "KREM_NOTIFY_OUTAGES": "false",
    "BOT_LIGHTBOT_TOKEN": "1:a",
    "BOT_SCHEDULEBOT_TOKEN": "2:b",
    "HA_WS_URL": "wss://ha.example/api/websocket",
    "HA_TOKEN": "token",
    "POWER_SOURCE": "kem",
    "WOL_MAC": "BC:FC:E7:1A:15:90",
}


@pytest.fixture
def settings():
    return load_settings(dict(BASE_ENV))
