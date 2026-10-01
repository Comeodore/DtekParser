from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional, Union

from app.dtek.models import DaySchedule, Outage


class ScheduleChange(str, Enum):
    PUBLISHED = "published"   # outages appeared for a day that had none (or was unknown)
    CHANGED = "changed"       # outages were there and are now different
    CLEARED = "cleared"       # outages were there and are gone (or the day was withdrawn)


@dataclass(frozen=True)
class ScheduleEvent:
    day: date
    change: ScheduleChange
    schedule: Optional[DaySchedule]   # None when DTEK withdrew the day entirely
    previous: Optional[DaySchedule]


class OutageChange(str, Enum):
    STARTED = "started"
    UPDATED = "updated"                 # kind or reason changed
    RESTORATION_CHANGED = "restoration_changed"
    ENDED = "ended"


@dataclass(frozen=True)
class OutageEvent:
    change: OutageChange
    outage: Optional[Outage]     # None for ENDED
    previous: Optional[Outage]


Event = Union[ScheduleEvent, OutageEvent]
