"""The inverter's last reading in Prometheus' text format, for VictoriaMetrics on the LAN (`/metrics`).

Every sample carries the logger's own collection time, so a scrape with
honor_timestamps keeps one point per upload (~72 s apart) at the moment it
was measured, however often it scrapes: a reading seen again is the same
point, and when the logger is silent the series stop rather than repeat.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.inverter.deye import InverterMonitor

# name → (help, the reading's attribute)
GAUGES = {
    "deye_pv_power_watts": ("Solar generation", "pv_w"),
    "deye_load_power_watts": ("What the house consumes", "load_w"),
    "deye_battery_power_watts": ("Battery power, positive while charging", "battery_w"),
    "deye_battery_soc_percent": ("Battery charge", "soc"),
    "deye_battery_voltage_volts": ("Battery voltage", "battery_v"),
    "deye_grid_power_watts": ("Drawn from the grid, negative when feeding in", "grid_w"),
    "deye_grid_voltage_volts": ("Grid voltage", "grid_v"),
    "deye_grid_frequency_hertz": ("Grid frequency", "grid_hz"),
    "deye_pv_today_kwh": ("The panels' output since the inverter's midnight", "pv_today_kwh"),
    "deye_load_today_kwh": ("Consumed since the inverter's midnight", "load_today_kwh"),
    "deye_grid_today_kwh": ("Drawn from the grid since the inverter's midnight", "grid_today_kwh"),
    "deye_battery_charged_today_kwh": ("Into the battery since the inverter's midnight", "charged_today_kwh"),
    "deye_battery_discharged_today_kwh": ("Out of the battery since the inverter's midnight", "discharged_today_kwh"),
}


def _value(v: float) -> str:
    return repr(float(v))


def exposition(monitor: Optional[InverterMonitor]) -> str:
    """Nothing until the first reading; a point Deye left out is left out here too."""
    reading = monitor.latest if monitor is not None else None
    if reading is None:
        return ""
    # Not "site": VictoriaMetrics adds its own site label to everything it scrapes.
    source = f'source="{monitor.source}"'
    ms =int(reading.at.timestamp() * 1000)
    lines = []

    def family(name: str, help_: str, samples: list[tuple[str, Optional[float]]], stamped: bool = True) -> None:
        samples = [(labels, v) for labels, v in samples if v is not None]
        if not samples:
            return
        lines.extend((f"# HELP {name} {help_}", f"# TYPE {name} gauge"))
        for labels, v in samples:
            lines.append(f"{name}{{{labels}}} {_value(v)}" + (f" {ms}" if stamped else ""))

    for name, (help_, attr) in GAUGES.items():
        family(name, help_, [(source, getattr(reading, attr))])
    family("deye_grid_present", "1 while the grid's voltage is there", [(source, 1.0 if reading.grid_present else 0.0)])
    family("deye_pv_string_power_watts", "Solar generation per PV input",
           [(f'{source},string="{n}"', w) for n, (w, _) in enumerate(reading.pv_strings, 1)])
    family("deye_pv_string_voltage_volts", "Voltage per PV input",
           [(f'{source},string="{n}"', v) for n, (_, v) in enumerate(reading.pv_strings, 1)])
    split = monitor.grid_day.split(reading)
    if split is not None:
        family("deye_grid_tariff_today_kwh", "Drawn from the grid today by tariff",
               [(f'{source},tariff="night"', split[0]), (f'{source},tariff="day"', split[1])])
    # Stamped with the scrape, so its age shows how fresh the data is.
    family("deye_reading_timestamp_seconds", "When the logger collected the last reading",
           [(source, reading.at.timestamp())], stamped=False)
    return "\n".join(lines) + "\n"
