from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel


class CellStatus(str, Enum):
    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    POWER_OFF_FIRST_30 = "power_off_first_30"
    POWER_OFF_SECOND_30 = "power_off_second_30"


class OutageStatus(str, Enum):
    POWER_ON = "power_on"
    SCHEDULED = "scheduled"
    EMERGENCY = "emergency"


class HourSlot(BaseModel):
    hour: int
    status: CellStatus


class CurrentOutage(BaseModel):
    status: OutageStatus
    reason: str = ""
    start_time: str = ""
    restoration_time: str = ""
    last_updated: str = ""


class DaySchedule(BaseModel):
    date: str
    slots: list[HourSlot]
    last_updated: str = ""


class ScheduleResponse(BaseModel):
    current_outage: Optional[CurrentOutage] = None
    today: Optional[DaySchedule] = None
    tomorrow: Optional[DaySchedule] = None
    updated_at: Optional[datetime] = None
    address: str = ""
    parser_type: str = ""


class HealthResponse(BaseModel):
    status: str
    database: bool
    parser: bool
    last_parse: Optional[datetime] = None
