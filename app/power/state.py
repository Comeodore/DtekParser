"""Power state machine: one stored state, transitions produce events.

Kept free of I/O so the subtle parts (unavailable sensors, transitions that
happened while we were disconnected) are covered by unit tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class PowerStatus(str, Enum):
    # Values stored in power_events.state since the first LightBot version.
    ON = "OK"
    OFF = "ALARM"


@dataclass(frozen=True)
class PowerState:
    status: PowerStatus
    since: datetime


@dataclass(frozen=True)
class PowerTransition:
    status: PowerStatus
    at: datetime
    previous_since: datetime
    battery: str
    # Seen in the snapshot HA sends on (re)subscribe rather than live.
    detected_late: bool

    @property
    def duration_seconds(self) -> float:
        return (self.at - self.previous_since).total_seconds()


# A change HA reports this long after it happened is shown with its own time.
LATE_THRESHOLD = timedelta(seconds=90)


def parse_voltage(raw: object, min_voltage: float) -> Optional[PowerStatus]:
    """None when the sensor has no usable value (HA restarting, integration down)."""
    try:
        voltage = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return PowerStatus.ON if voltage >= min_voltage else PowerStatus.OFF


def parse_battery(raw: object) -> str:
    try:
        return f"{round(float(raw))}%"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "N/A"


def next_state(
    stored: Optional[PowerState],
    status: PowerStatus,
    changed_at: datetime,
    now: datetime,
    battery: str,
    from_snapshot: bool,
) -> tuple[PowerState, Optional[PowerTransition]]:
    """Apply an observed status; returns the state to store and the transition, if any.

    `changed_at` is HA's last_changed of the voltage sensor. For an outage it is
    exact (the reading stays 0 V). For a restore seen only in a snapshot it is the
    last fluctuation, i.e. roughly "now", which is the best we know.
    """
    if stored is None:
        return PowerState(status, min(changed_at, now)), None
    if status == stored.status:
        return stored, None

    at = min(max(changed_at, stored.since), now)
    late = from_snapshot or now - at > LATE_THRESHOLD
    transition = PowerTransition(status, at, stored.since, battery, late)
    return PowerState(status, at), transition
