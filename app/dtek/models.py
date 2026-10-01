"""Domain model of what DTEK publishes for one address.

Everything here is immutable and (de)serialisable, because snapshots and the
detector baseline are persisted between restarts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional

from app.timeutil import parse_dtek_datetime

HOURS_IN_DAY = 24


class Slot(str, Enum):
    ON = "power_on"
    OFF = "power_off"
    FIRST_HALF_OFF = "power_off_first_30"
    SECOND_HALF_OFF = "power_off_second_30"

    @property
    def is_outage(self) -> bool:
        return self is not Slot.ON

    @property
    def off_minutes(self) -> tuple[int, int]:
        """Part of the hour without power, as (start, end) minutes; (0, 0) when powered."""
        return {
            Slot.ON: (0, 0),
            Slot.OFF: (0, 60),
            Slot.FIRST_HALF_OFF: (0, 30),
            Slot.SECOND_HALF_OFF: (30, 60),
        }[self]


@dataclass(frozen=True)
class Line:
    """Schedule of one DTEK group ("черга") for one day; slot index is the hour."""

    group: str
    name: str
    slots: tuple[Slot, ...]

    def __post_init__(self) -> None:
        if len(self.slots) != HOURS_IN_DAY:
            raise ValueError(f"{self.group}: expected {HOURS_IN_DAY} slots, got {len(self.slots)}")

    def has_outages(self, from_hour: int = 0) -> bool:
        return any(s.is_outage for s in self.slots[from_hour:])

    def to_dict(self) -> dict:
        return {"group": self.group, "name": self.name, "slots": [s.value for s in self.slots]}

    @classmethod
    def from_dict(cls, data: dict) -> "Line":
        return cls(data["group"], data["name"], tuple(Slot(s) for s in data["slots"]))


@dataclass(frozen=True)
class DaySchedule:
    """One day of the published schedule. A house fed by two lines has two."""

    date: date
    lines: tuple[Line, ...]

    def has_outages(self, from_hour: int = 0) -> bool:
        return any(line.has_outages(from_hour) for line in self.lines)

    def view(self, from_hour: int = 0) -> tuple[tuple[str, tuple[Slot, ...]], ...]:
        """What matters for change detection: groups and the slots from `from_hour` on."""
        return tuple((line.group, line.slots[from_hour:]) for line in self.lines)

    def to_dict(self) -> dict:
        return {"date": self.date.isoformat(), "lines": [line.to_dict() for line in self.lines]}

    @classmethod
    def from_dict(cls, data: dict) -> "DaySchedule":
        return cls(date.fromisoformat(data["date"]), tuple(Line.from_dict(x) for x in data["lines"]))


class OutageKind(str, Enum):
    EMERGENCY = "emergency"
    PLANNED = "planned"
    # Any other outage DTEK lists for the address, e.g. one applied by the schedule.
    OTHER = "other"


@dataclass(frozen=True)
class Outage:
    """The "power is off at your address" notice DTEK shows above the schedule."""

    kind: OutageKind
    reason: str
    start: str
    end: str
    # DTEK's national emergency mode: hourly schedules do not apply at all.
    schedules_suspended: bool = False

    @property
    def start_at(self) -> Optional[datetime]:
        return parse_dtek_datetime(self.start)

    @property
    def end_at(self) -> Optional[datetime]:
        return parse_dtek_datetime(self.end)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "start": self.start,
            "end": self.end,
            "schedules_suspended": self.schedules_suspended,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["Outage"]:
        if not data:
            return None
        return cls(
            OutageKind(data["kind"]),
            data.get("reason", ""),
            data.get("start", ""),
            data.get("end", ""),
            bool(data.get("schedules_suspended", False)),
        )


class Availability(str, Enum):
    """Whether DTEK publishes an hourly schedule for this address at all."""

    OK = "ok"
    NO_GROUP = "no_group"            # house is not assigned to any group
    BUILDING_MANAGED = "voluntary"   # schedule is handled by the building's management
    OTHER_OPERATOR = "cek"           # house is served by another operator


@dataclass(frozen=True)
class Snapshot:
    source: str
    fetched_at: datetime
    groups: tuple[str, ...]
    days: tuple[DaySchedule, ...]
    outage: Optional[Outage]
    outage_updated: str     # DTEK: "Дата оновлення інформації"
    schedule_updated: str   # DTEK: "Дата та час останнього оновлення інформації на графіку"
    availability: Availability = Availability.OK

    def day(self, day: date) -> Optional[DaySchedule]:
        return next((d for d in self.days if d.date == day), None)

    @property
    def schedules_suspended(self) -> bool:
        return self.outage is not None and self.outage.schedules_suspended

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat(),
            "groups": list(self.groups),
            "days": [d.to_dict() for d in self.days],
            "outage": self.outage.to_dict() if self.outage else None,
            "outage_updated": self.outage_updated,
            "schedule_updated": self.schedule_updated,
            "availability": self.availability.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        return cls(
            source=data["source"],
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
            groups=tuple(data.get("groups", ())),
            days=tuple(DaySchedule.from_dict(d) for d in data.get("days", ())),
            outage=Outage.from_dict(data.get("outage")),
            outage_updated=data.get("outage_updated", ""),
            schedule_updated=data.get("schedule_updated", ""),
            availability=Availability(data.get("availability", Availability.OK.value)),
        )
