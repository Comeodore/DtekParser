"""Telegram texts for the owner (HTML, Ukrainian like the rest of the krem bot): a day's final table, or an alarm."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from typing import Optional, Sequence

from deye_reserve.inputs import Forecast, Schedule
from deye_reserve.planner import Plan, Slot, Table


@dataclass
class Report:
    """Everything one replan knew, for the journal and /plan."""
    now: datetime
    table: Table
    plan: Optional[Plan] = None
    fallback: Optional[str] = None          # why the safe table was chosen instead of a plan
    schedule: Optional[Schedule] = None
    forecast: Optional[Forecast] = None
    soc: Optional[float] = None
    grid_present: Optional[bool] = None
    reading_fresh: bool = False             # the inverter's logger is online
    notes: list[str] = field(default_factory=list)


def table_line(table: Sequence[Slot]) -> str:
    return " · ".join(f"{s.time} {s.soc}%{' ⚡' if s.grid_charge else ''}" for s in table)


def table_block(table: Sequence[Slot]) -> str:
    rows = [f"{s.time}  {s.soc:>3}%{'  ⚡ заряд від мережі' if s.grid_charge else ''}" for s in table]
    return "<pre>" + "\n".join(rows) + "</pre>"


def day_title(day: date, today: date) -> str:
    if day == today:
        return f"на сьогодні, {day:%d.%m}"
    if day == today + timedelta(days=1):
        return f"на завтра, {day:%d.%m}"
    return f"на {day:%d.%m}"


# A table in the chat is a table in the inverter: only the other cases get a line.
OUTCOME = {
    "off": "ℹ️ Лише рекомендація: запис вимкнено",
    "already": "",
    "written": "",
    "blocked": "⏸ Запишу, щойно буде можна: {}",
    "failed": "🚨 Не вдалося записати: {}",
}


def render_day(day: date, today: date, report: Report, outcome: tuple[str, str]) -> str:
    """Only the final table of the day whose schedule came out; a line only when it is not in the inverter."""
    lines = [f"🔋 <b>Таблиця TOU {day_title(day, today)}</b>"]
    if report.fallback:
        lines.append(f"⚠️ Безпечна таблиця 90/45: {escape(report.fallback)}")
    lines.append(table_block(report.table))
    kind, detail = outcome
    if OUTCOME[kind]:
        lines.append(escape(OUTCOME[kind].format(detail)))
    return "\n".join(lines)


def render_alarm(table: Sequence[Slot], detail: str) -> str:
    return f"🚨 <b>Запис TOU не вдався</b>\n{escape(detail)}\nПланувалось: {escape(table_line(table))}"
