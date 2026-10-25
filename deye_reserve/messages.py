"""Telegram texts for the owner (HTML, Ukrainian like the rest of the krem bot): a day's final table and why it
changed, or an alarm."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from typing import Optional, Sequence

from deye_reserve.inputs import Forecast, Schedule, changed_days
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


def day_title(day: date, today: date, last: Optional[date] = None) -> str:
    """"на сьогодні, 19.10"; with `last`, the days from `day` to `last` share one table: "на сьогодні і завтра,
    19–20.10"."""
    if last is not None and last != day:
        span = f"{day:%d}–{last:%d.%m}" if day.month == last.month else f"{day:%d.%m}–{last:%d.%m}"
        if day == today and last == today + timedelta(days=1):
            return f"на сьогодні і завтра, {span}"
        return f"на {span}"
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


def render_days(days: Sequence[date], today: date, report: Report, outcome: tuple[str, str],
                reasons: Sequence[str] = ()) -> str:
    """Each day's own final table; days in a row with the same table share one. The one going into the inverter
    now gets a line only when it did not go in; tomorrow's, when it is not in yet, says when it will be. Why it
    changed goes under it all, a line apart."""
    groups: list[list[date]] = []
    for day in days:
        if groups and groups[-1][-1] + timedelta(days=1) == day and \
                tuple(report.tables.get(day, report.table)) == tuple(report.tables.get(groups[-1][0], report.table)):
            groups[-1].append(day)
        else:
            groups.append([day])
    blocks = []
    for i, group in enumerate(groups):
        day = group[0]
        table = report.tables.get(day, report.table)
        lines = [f"🔋 <b>Таблиця TOU {day_title(day, today, group[-1])}</b>"]
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
    if reasons:
        blocks.append("Причина: " + escape("; ".join(reasons)))
    return "\n\n".join(blocks)


# ---- the same news in the log, a line a day ----

def range_line(table: Sequence[Slot]) -> str:
    """A table as the krem mini app shows it: slots in a row with the same target and charge are one range, and
    the last runs on past midnight into the first when they are the same: "07–23 45% · 23–07 70% ⚡"."""
    out: list[list] = []
    for s in sorted(table, key=lambda s: s.start):
        if out and (out[-1][2], out[-1][3]) == (s.soc, s.grid_charge):
            continue
        if out:
            out[-1][1] = s.start
        out.append([s.start, None, s.soc, s.grid_charge])
    if len(out) == 1:
        out = [[0, 24 * 60, out[0][2], out[0][3]]]
    elif out:
        out[-1][1] = out[0][0] or 24 * 60
        if (out[0][2], out[0][3]) == (out[-1][2], out[-1][3]):
            out[-1][1] = out.pop(0)[1]
    whole = all(m % 60 == 0 for r in out for m in r[:2])

    def hm(m: int) -> str:
        return f"{m // 60:02d}" if whole else f"{m // 60:02d}:{m % 60:02d}"
    return " · ".join(f"{hm(a)}–{hm(b)} {soc}%{' ⚡' if charge else ''}" for a, b, soc, charge in out)


def log_line(day: date, today: date, report: Report, outcome: tuple[str, str], reasons: Sequence[str],
             chat: str) -> str:
    """A day's table news for the log, whether or not it went to the chat: "table for 26.10: 07–23 45% · 23–07
    70% ⚡; in the inverter; Причина: …; chat: off"."""
    table = report.tables.get(day, report.table)
    kind, detail = outcome
    if tuple(table) == tuple(report.table):
        where = {"off": "a recommendation, writing is off", "already": "in the inverter", "written": "in the inverter",
                 "blocked": f"not in the inverter yet: {detail}", "failed": f"write failed: {detail}"}[kind]
    elif day > today and report.switch_at is not None:
        at = local(report.switch_at)
        where = f"takes over at {at:%H:%M}" if at.date() == today else "takes over after 00:00"
    else:
        where = "not in the inverter"
    parts = [f"table for {day:%d.%m}: {range_line(table)}", where]
    if report.fallback:
        parts.append(f"fallback: {report.fallback}" + (f" (plan of {local(report.kept_from):%H:%M} kept)"
                                                      if report.kept_from is not None else ""))
    parts += [f"Причина: {'; '.join(reasons)}" if reasons else "Причина: —", f"chat: {chat}"]
    return "; ".join(parts)


# ---- why a table changed ----

QUIET = ["power_on"] * 24
SUN_STEP = 0.3          # kWh a day: less is noise of a new forecast
LOAD_STEP = 0.3         # kWh a day
SOC_STEP = 5.0          # % off the plan the last message had


def change_reasons(then: dict, ctx: dict, today: date, soc: Optional[float], night_over: bool = False,
                   unpublished: Optional[date] = None, restarted: bool = False,
                   expected_soc: Optional[float] = None, generic: bool = True) -> list[str]:
    """What changed between `then` (the replan before, or the last table message) and now (`ctx`) — see
    ReserveService._context. `night_over`: a night ended since, and only the night slots changed;
    `unpublished`: tomorrow's table goes out with no DTEK schedule for it yet; `expected_soc`: the
    battery the plan last shown in the chat expected by now. Without `generic`, nothing found is []."""
    if ctx.get("fallback"):
        return ["немає даних для плану"]
    out = []
    if then.get("fallback"):
        out.append("дані для плану знову є")
    if "schedule" in then and "schedule" in ctx:
        new = {date.fromisoformat(d): tuple(s) for d, s in ctx["schedule"].items()}
        for d in changed_days(then["schedule"], new, today):
            old_slots, new_slots = then["schedule"].get(d.isoformat(), QUIET), list(new.get(d, QUIET))
            if old_slots == QUIET:
                out.append(f"опубліковано графік на {d:%d.%m}")
            elif new_slots == QUIET:
                out.append(f"скасовано відключення на {d:%d.%m}")
            else:
                out.append(f"змінився графік на {d:%d.%m}")
    if unpublished is not None:
        out.append(f"графік на {unpublished:%d.%m} ще не опубліковано: план без відключень")
    if then.get("calibration") is False and ctx.get("calibration"):
        out.append("калібрувальний заряд до 100%")
    elif then.get("calibration") and ctx.get("calibration") is False:
        out.append("батарея зарядилась до 100%: калібрування вже не потрібне" if soc is not None and soc >= 99
                   else "калібрування вже не потрібне")
    if night_over:
        out.append("нічний заряд позаду: нічні слоти тепер на наступну ніч")
    sun_new = then.get("forecast_at") and ctx.get("forecast_at") and then["forecast_at"] != ctx["forecast_at"]
    if sun_new:
        for d, (p50, low) in sorted(ctx.get("sun", {}).items()):
            old = then.get("sun", {}).get(d)
            if old and (abs(p50 - old[0]) >= SUN_STEP or abs(low - old[1]) >= SUN_STEP):
                out.append(f"прогноз сонця на {date.fromisoformat(d):%d.%m}: {old[0]:.1f} → {p50:.1f} кВт·год "
                           f"(песимістично {old[1]:.1f} → {low:.1f})")
    if then.get("load_at") and ctx.get("load_at") and then["load_at"] != ctx["load_at"] and \
            abs(ctx["load_kwh"] - then["load_kwh"]) >= LOAD_STEP:
        out.append(f"споживання будинку: {then['load_kwh']:.1f} → {ctx['load_kwh']:.1f} кВт·год на добу")
    if soc is not None and expected_soc is not None and abs(soc - expected_soc) >= SOC_STEP:
        out.append(f"заряд {soc:.0f}% замість очікуваних {expected_soc:.0f}%")
    if not out and generic:
        if sun_new:
            out.append("оновився прогноз сонця")
        elif restarted:
            out.append("перезапуск сервісу")
        else:
            out.append(f"перерахунок за поточним зарядом {soc:.0f}%" if soc is not None else "перерахунок плану")
    return out


def night_line(ctx: dict, day: date) -> str:
    """The day a night's charge is for, with that day's sun: the charge is about it, not about today."""
    sun = ctx.get("sun", {}).get(day.isoformat())
    if not sun:
        return f"ніч під {day:%d.%m}"
    return f"ніч під {day:%d.%m}: прогноз сонця {sun[0]:.1f} кВт·год (песимістично {sun[1]:.1f})"


def render_battery(soc: float, load_w: float, empty_at: datetime, back_at: Optional[datetime]) -> str:
    """The battery will not last until the grid is due back at the house's present load. For every krem chat;
    no full stops at the ends of lines."""
    lines = ["🪫 <b>Батареї може не вистачити до ввімкнення світла</b>",
             f"Заряд {soc:.0f}%, навантаження ~{load_w:.0f} Вт: вистачить приблизно до {local(empty_at):%H:%M}"]
    lines.append(f"За графіком світло з'явиться о {local(back_at):%H:%M}" if back_at is not None
                 else "Світла немає поза графіком")
    lines.append("Варто зменшити навантаження")
    return "\n".join(lines)


def render_charge(samples: Sequence[list], params) -> str:
    """The first grid charge with Grid Charge 80 A and Grid Peak Shaving: what the grid gave, what the battery
    took, and whether the import stayed under the limit. Samples: [at, battery W, grid W, house W, SOC]."""
    first, last = datetime.fromisoformat(samples[0][0]), datetime.fromisoformat(samples[-1][0])

    def mean(i: int) -> float:
        return sum(s[i] for s in samples) / len(samples) / 1000
    peak = max(s[2] for s in samples) / 1000
    limit = params.grid_limit_kw
    lines = ["⚡ <b>Заряд від мережі з новими налаштуваннями</b>",
             f"Grid Charge 80 А, Peak Shaving {limit:.1f} кВт; {local(first):%H:%M}–{local(last):%H:%M}:",
             f"Імпорт із мережі: {mean(2):.2f} кВт у середньому, найбільше {peak:.2f}",
             f"Заряд батареї: {mean(1):.2f} кВт ({samples[0][4]:.0f} → {samples[-1][4]:.0f}%)",
             f"Будинок: {mean(3):.2f} кВт"]
    lines.append(f"✅ Peak Shaving тримає імпорт у межах {limit:.1f} кВт" if peak <= limit + 0.1
                 else f"⚠️ Імпорт доходив до {peak:.2f} кВт — більше за {limit:.1f}")
    if mean(1) < 3.0 and mean(2) < limit - 0.5:
        lines.append("Заряд помітно менший за 80 А (~4.2 кВт), хоча до ліміту імпорту є запас: "
                     "схоже, обмежує BMS батареї")
    return "\n".join(lines)


def render_alarm(table: Sequence[Slot], detail: str, current: Optional[Sequence[Slot]] = None) -> str:
    lines = ["🚨 <b>Запис TOU не вдався</b>", escape(detail), "Планувалось:", table_block(table)]
    if current is not None:
        lines += ["У інверторі зараз:", table_block(current)]
    return "\n".join(lines)
