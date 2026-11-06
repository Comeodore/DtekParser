"""One Deye inverter's reading as device/latest gives it (developer.deyecloud.com). Free of I/O: used by
InverterMonitor here and by dtek-brain (edge/), which shows the inverters in the mini app it stands in for.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.timeutil import KYIV_TZ

# A reading older than this is shown greyed out, and says nothing about the grid.
STALE_S = 15 * 60


class DeyeError(Exception):
    pass


@dataclass(frozen=True)
class InverterReading:
    at: datetime                    # when the logger collected it
    soc: Optional[float]            # battery charge, %
    battery_w: Optional[float]      # positive while charging, negative while discharging
    load_w: Optional[float]         # what the house consumes
    pv_w: Optional[float]           # solar generation
    grid_w: Optional[float]         # drawn from the grid; negative when feeding in
    grid_v: Optional[float]
    grid_present: bool
    pv_today_kwh: Optional[float] = None    # the panels' output since the inverter's midnight
    grid_today_kwh: Optional[float] = None  # drawn from the grid since the inverter's midnight
    # The rest is for /metrics only, not the mini app.
    load_today_kwh: Optional[float] = None
    charged_today_kwh: Optional[float] = None       # into the battery since the inverter's midnight
    discharged_today_kwh: Optional[float] = None
    battery_v: Optional[float] = None
    grid_hz: Optional[float] = None
    pv_strings: tuple[tuple[Optional[float], Optional[float]], ...] = ()     # (W, V) per PV input

    def to_dict(self, now: datetime, stale_after_s: float) -> dict:
        return {
            "at": self.at.isoformat(),
            # Time only, even for yesterday's reading: the stale flag is what says it is old.
            "time": self.at.astimezone(KYIV_TZ).strftime("%H:%M"),
            "stale": (now - self.at).total_seconds() > stale_after_s,
            "soc": self.soc,
            "battery_w": self.battery_w,
            "load_w": self.load_w,
            "pv_w": self.pv_w,
            "grid_w": self.grid_w,
            "grid_v": self.grid_v,
            "grid_present": self.grid_present,
            "pv_today_kwh": self.pv_today_kwh,
            "grid_today_kwh": self.grid_today_kwh,
        }


def parse_device_latest(item: dict, min_grid_voltage: float) -> InverterReading:
    """One entry of device/latest's deviceDataList. Single-phase inverters name grid points L1L2."""
    values = {p.get("key"): p.get("value") for p in item.get("dataList") or []}

    def num(*keys: str) -> Optional[float]:
        for key in keys:
            try:
                return float(values[key])
            except (KeyError, TypeError, ValueError):
                continue
        return None

    collected = item.get("collectionTime")
    if not collected:
        raise DeyeError(f"inverter {item.get('deviceSn')} has no collectionTime")
    battery = num("BatteryPower")
    grid_v = num("GridVoltageL1L2", "GridVoltageL1")
    strings = []
    for n in range(1, 9):
        if f"DCPowerPV{n}" not in values and f"DCVoltagePV{n}" not in values:
            break
        strings.append((num(f"DCPowerPV{n}"), num(f"DCVoltagePV{n}")))
    return InverterReading(
        at=datetime.fromtimestamp(int(collected), KYIV_TZ),
        soc=num("SOC"),
        # Deye reports battery power negative while charging.
        battery_w=-battery if battery is not None else None,
        load_w=num("TotalConsumptionPower"),
        pv_w=num("TotalDCInputPower"),
        grid_w=num("TotalGridPower"),
        grid_v=grid_v,
        grid_present=grid_v is not None and grid_v >= min_grid_voltage,
        pv_today_kwh=num("PVDailyPowerGenerationActive"),
        grid_today_kwh=num("DailyEnergyPurchased"),
        load_today_kwh=num("DailyConsumption"),
        charged_today_kwh=num("DailyChargingEnergy"),
        discharged_today_kwh=num("DailyDischargingEnergy"),
        battery_v=num("BatteryVoltage"),
        grid_hz=num("GridFrequency"),
        pv_strings=tuple(strings),
    )
