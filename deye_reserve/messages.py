"""Telegram texts for the owner (HTML), in Ukrainian like the rest of the krem bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from typing import Optional, Sequence

from deye_reserve.inputs import Forecast, Schedule
from deye_reserve.planner import Params, Plan, Slot, Table, local, minute_of_day


@dataclass
class Report:
    """Everything one replan knew, for the message and /plan."""
    now: datetime
    table: Table
    plan: Optional[Plan] = None
    fallback: Optional[str] = None          # why the safe table was chosen instead of a plan
    schedule: Optional[Schedule] = None
    forecast: Optional[Forecast] = None
    soc: Optional[float] = None
    grid_present: Optional[bool] = None
    inverter_table: Optional[Table] = None  # as read from Deye; None when unknown
    write_note: str = ""
    notes: list[str] = field(default_factory=list)


def num(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def day_label(d: date, today: date) -> str:
    if d == today:
        return "сьогодні"
    if d == today + timedelta(days=1):
        return "завтра"
    if d == today - timedelta(days=1):
        return "вчора"
    return d.strftime("%d.%m")


def span(a: datetime, b: datetime, today: date, with_day: bool = True) -> str:
    la, lb = local(a), local(b)
    end = "24:00" if lb.hour == 0 and lb.minute == 0 and lb.date() == la.date() + timedelta(days=1) else lb.strftime("%H:%M")
    prefix = f"{day_label(la.date(), today)} " if with_day else ""
    return f"{prefix}{la:%H:%M}–{end}"


def table_line(table: Sequence[Slot]) -> str:
    return " · ".join(f"{s.time} {s.soc}%{' ⚡' if s.grid_charge else ''}" for s in table)


def table_block(table: Sequence[Slot]) -> str:
    rows = [f"{s.time}  {s.soc:>3}%{'  ⚡ заряд від мережі' if s.grid_charge else ''}" for s in table]
    return "<pre>" + "\n".join(rows) + "</pre>"


def _schedule_lines(schedule: Schedule, today: date) -> list[str]:
    lines = []
    for d in (today, today + timedelta(days=1)):
        if d not in schedule.days:
            lines.append(f"  {day_label(d, today)} {d:%d.%m}: ще не опубліковано")
            continue
        outs = [(a, b) for a, b in schedule.outages if local(a).date() == d]
        text = ", ".join(span(a, b, today, with_day=False) for a, b in outs) or "без відключень"
        lines.append(f"  {day_label(d, today)} {d:%d.%m}: {escape(text)}")
    return lines


def _charges(plan: Plan, params: Params, today: date) -> list[str]:
    """Day slots with grid charge: say which outage each prepares for."""
    out = []
    table = plan.table
    for i, slot in enumerate(table):
        if not slot.grid_charge:
            continue
        end = table[i + 1].start if i + 1 < len(table) else 24 * 60
        night = params.night_start_h * 60 <= slot.start or end <= params.night_end_h * 60
        if night and slot.soc == plan.night:
            continue
        target = next((o for o in plan.outages if minute_of_day(o[0]) == end % (24 * 60) and o[0] > plan.start), None)
        why = f" перед відключенням {span(*target, today)}" if target else ""
        out.append(f"• заряд {slot.time}–{hhmm(end)} до {slot.soc}%{why}")
    return out


def _why_night(plan: Plan, params: Params) -> str:
    if plan.calibration:
        return f"• ніч {plan.night}%: калібрування батареї (повний заряд раз на тиждень)"
    if plan.kept_previous:
        return f"• ніч {plan.night}%: лишаю попередню таблицю — нова дала б менше {num(params.keep_previous_uah)} грн"
    lower = plan.candidates.get(plan.night - params.night_step)
    if plan.night <= params.night_min or lower is None:
        return f"• ніч {plan.night}%: мінімум, більше не потрібно"
    safe, cost, low = lower
    if not safe:
        return (f"• ніч {plan.night}%: з {plan.night - params.night_step}% у песимістичному сценарії "
                f"лишилося б {round(low)}% у відключенні (треба ≥{round(params.outage_min_soc)}%)")
    return (f"• ніч {plan.night}%: з {plan.night - params.night_step}% дорожче на "
            f"{num(cost - plan.expected.cost_adj)} грн (більше купівлі вдень)")


def render_plan(r: Report, params: Params) -> str:
    today = local(r.now).date()
    lines = []
    if r.fallback:
        lines.append("🔋 <b>Батарея: безпечна таблиця 90/45</b>")
        lines.append(f"⚠️ {escape(r.fallback)}")
    else:
        lines.append(f"🔋 <b>План батареї на ніч {r.plan.night}%</b>")
    if r.schedule is not None:
        lines.append("")
        lines.append("📅 Графік:")
        lines += _schedule_lines(r.schedule, today)
    if r.forecast is not None:
        parts = []
        for d in (today, today + timedelta(days=1)):
            if (d, 12) in r.forecast.p50:
                from_hour = local(r.now).hour if d == today else 0
                rest = " (решта)" if d == today and from_hour > 6 else ""
                parts.append(f"{day_label(d, today)}{rest} {num(r.forecast.total_kwh(d, False, from_hour))} / "
                             f"{num(r.forecast.total_kwh(d, True, from_hour))}")
        if parts:
            lines.append(f"☀️ Сонце, кВт·год (P50 / песимістично): {'; '.join(parts)}")
    if r.soc is not None:
        grid = "мережа є" if r.grid_present else "мережі немає"
        lines.append(f"🔌 Зараз: батарея {round(r.soc)}%, {grid}")
    lines.append("")
    lines.append("<b>Таблиця TOU</b>")
    lines.append(table_block(r.table))
    if r.plan is not None and not r.fallback:
        p, e, s = r.plan, r.plan.expected, r.plan.pessimistic
        lines.append("<b>Чому</b>")
        lines.append(_why_night(p, params))
        lines += _charges(p, params, today)
        judged = [k for k, (a, _) in enumerate(p.outages) if a >= p.start]
        if judged:
            exp_min = min(e.min_by.get(k, 100.0) for k in judged)
            pess_min = min(s.min_by.get(k, 100.0) for k in judged)
            lines.append(f"• мін. заряд у відключеннях: очікувано {round(exp_min)}%, песимістично {round(pess_min)}%")
        lines.append(f"• очікувано купити: вдень {num(e.buy_day)} кВт·год, вночі {num(e.buy_night)} "
                     f"≈ {num(e.cost, 0)} грн" + (f", втрачено сонця {num(e.curtailed)}" if e.curtailed >= 0.1 else ""))
        b = p.baseline
        if b is not None and b.table != p.table:
            # Both priced with the energy left in the battery at the end, at the night tariff.
            diff = b.expected.cost_adj - e.cost_adj
            if diff >= 0.05:
                lines.append(f"• проти постійних 90/45: дешевше на {num(diff)} грн")
            elif diff <= -0.05:
                lines.append(f"• постійні 90/45 дешевші на {num(-diff)} грн" +
                             ("" if b.safe else ", але не тримають відключення"))
        if not p.safe:
            lines.append("⚠️ Навіть з найбільшим зарядом у песимістичному сценарії батареї не вистачає на все відключення")
    for note in r.notes:
        lines.append(f"ℹ️ {escape(note)}")
    if r.inverter_table is not None:
        same = tuple(r.inverter_table) == tuple(r.table)
        lines.append("")
        lines.append("У інверторі зараз: " + ("та сама таблиця" if same else escape(table_line(r.inverter_table))))
    if r.write_note:
        lines.append(r.write_note)
    return "\n".join(lines)


def render_write(ok: bool, detail: str, table: Sequence[Slot]) -> str:
    if ok:
        return f"✅ <b>Таблицю TOU записано</b>\n{escape(table_line(table))}"
    return f"🚨 <b>Запис TOU не вдався</b>\n{escape(detail)}\nПланувалось: {escape(table_line(table))}"
