"""Telegram texts for the owner (HTML, Ukrainian like the rest of the krem bot): a day's final table, or an alarm."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from typing import Optional, Sequence

from deye_reserve.inputs import Forecast, Schedule
from deye_reserve.planner import Plan, Slot, Table, local


@dataclass
class Report:
    """Everything one replan knew, for the journal and /plan."""
    now: datetime
    table: Table                            # what the inverter should hold now
    tables: dict[date, Table] = field(default_factory=dict)    # every day's own table
    switch_at: Optional[datetime] = None    # when tomorrow's table takes over
    plan: Optional[Plan] = None
    fallback: Optional[str] = None          # why there is no fresh plan
    kept_from: Optional[datetime] = None    # ...and the earlier plan held instead of the safe table
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


def fallback_line(report: Report) -> str:
    if report.kept_from is not None:
        return f"⚠️ Тримаю план від {local(report.kept_from):%H:%M}: {escape(report.fallback)}"
    return f"⚠️ Безпечна таблиця 90/45: {escape(report.fallback)}"


def render_days(days: Sequence[date], today: date, report: Report, outcome: tuple[str, str]) -> str:
    """Each day's own final table. The one going into the inverter now gets a line only when it did not go in;
    tomorrow's, when it is not in yet, says when it will be."""
    blocks = []
    for i, day in enumerate(days):
        table = report.tables.get(day, report.table)
        lines = [f"🔋 <b>Таблиця TOU {day_title(day, today)}</b>"]
        if report.fallback and i == 0:
            lines.append(fallback_line(report))
        lines.append(table_block(table))
        kind, detail = outcome
        if tuple(table) == tuple(report.table):
            if OUTCOME[kind]:
                lines.append(escape(OUTCOME[kind].format(detail)))
        elif day > today and kind != "off" and report.switch_at is not None:
            at = local(report.switch_at)
            lines.append(f"⏭ Застосую о {at:%H:%M}" if at.date() == today else "⏭ Застосую після 00:00")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_battery(soc: float, load_w: float, empty_at: datetime, back_at: Optional[datetime]) -> str:
    """The battery will not last until the grid is due back at the house's present load."""
    lines = ["🪫 <b>Батареї може не вистачити до світла</b>",
             f"Заряд {soc:.0f}%, навантаження ~{load_w:.0f} Вт: вистачить приблизно до {local(empty_at):%H:%M}."]
    lines.append(f"За графіком світло о {local(back_at):%H:%M}." if back_at is not None
                 else "Світла немає поза графіком.")
    lines.append("Варто зменшити навантаження.")
    return "\n".join(lines)


def render_alarm(table: Sequence[Slot], detail: str, current: Optional[Sequence[Slot]] = None) -> str:
    lines = ["🚨 <b>Запис TOU не вдався</b>", escape(detail), "Планувалось:", table_block(table)]
    if current is not None:
        lines += ["У інверторі зараз:", table_block(current)]
    return "\n".join(lines)
