"""Telegram texts (HTML parse mode). Everything coming from DTEK is escaped."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
from typing import Optional

from app.dtek.models import DaySchedule, Line, Outage, OutageKind, Snapshot
from app.engine.events import OutageChange, OutageEvent, ScheduleChange, ScheduleEvent
from app.engine.periods import Period, continues_into, current_period, next_period, upcoming_periods
from app.power.state import PowerStatus, PowerTransition
from app.timeutil import KYIV_TZ, format_duration, format_moment, hhmm, minute_of_day, parse_dtek_datetime

TEXT = {
    "en": {
        "today_published": "Today's schedule published",
        "today_changed": "Today's schedule updated",
        "tomorrow_published": "Tomorrow's schedule published",
        "tomorrow_changed": "Tomorrow's schedule updated",
        "tomorrow_cleared": "Tomorrow's outages cancelled",
        "no_more_today": "✅ No more outages today",
        "no_outages": "✅ No outages planned",
        "line": "Line",
        "tomorrow_suffix": "tomorrow",
        "emergency": "Emergency outage",
        "planned": "Planned outage",
        "other": "Outage at the address",
        "updated": "Outage details updated",
        "restoration_changed": "Restoration time changed",
        "ended": "DTEK no longer reports an outage at the address",
        "reason": "Reason",
        "since": "Since",
        "restoration": "Restoration",
        "until": "until",
        "suspended": "⚠️ Hourly schedules don't apply right now, the restoration time may change.",
        "next_outage": "Next outage",
        "power_off": "POWER OUTAGE",
        "power_on": "POWER RESTORED",
        "uptime": "Uptime",
        "duration": "Outage duration",
        "battery": "Battery",
        "scheduled": "⚡ Scheduled",
        "unscheduled": "⚠️ Unscheduled outage",
        "dtek": "DTEK",
        "late_off": "ℹ️ Noticed after a reconnect, power went off at {time}",
        "late_on": "ℹ️ Noticed after a reconnect at {time}",
    },
    "uk": {
        "today_published": "Графік на сьогодні опубліковано",
        "today_changed": "Графік на сьогодні оновлено",
        "tomorrow_published": "Графік на завтра опубліковано",
        "tomorrow_changed": "Графік на завтра оновлено",
        "tomorrow_cleared": "Відключення на завтра скасовано",
        "no_more_today": "✅ Більше відключень сьогодні не заплановано",
        "no_outages": "✅ Відключень не заплановано",
        "line": "Лінія",
        "tomorrow_suffix": "завтра",
        "emergency": "Аварійне відключення",
        "planned": "Планове відключення",
        "other": "Відключення за адресою",
        "updated": "Оновлено інформацію про відключення",
        "restoration_changed": "Змінився час відновлення",
        "ended": "ДТЕК більше не повідомляє про відключення за адресою",
        "reason": "Причина",
        "since": "Початок",
        "restoration": "Відновлення",
        "until": "до",
        "suspended": "⚠️ Графіки погодинних відключень зараз не діють, час відновлення може змінюватися.",
        "next_outage": "Наступне відключення",
        "power_off": "СВІТЛО ЗНИКЛО",
        "power_on": "СВІТЛО З'ЯВИЛОСЯ",
        "uptime": "Було світло",
        "duration": "Тривалість відключення",
        "battery": "Батарея",
        "scheduled": "⚡ За графіком",
        "unscheduled": "⚠️ Поза графіком",
        "dtek": "ДТЕК",
        "late_off": "ℹ️ Помічено після перепідключення, світло зникло о {time}",
        "late_on": "ℹ️ Помічено після перепідключення о {time}",
    },
}

OUTAGE_ICONS = {OutageKind.EMERGENCY: "🚨", OutageKind.PLANNED: "🛠", OutageKind.OTHER: "⚡"}


def _t(lang: str, key: str) -> str:
    return TEXT[lang][key]


def _today(now: datetime) -> date:
    return now.astimezone(KYIV_TZ).date()


# ---------------------------------------------------------------- schedules

def _line_header(lang: str, index: int, line: Line) -> str:
    return f"<b>{_t(lang, 'line')} {index + 1}</b> · {escape(line.name)}"


def _period_text(lang: str, period: Period, line: Line, next_day: Optional[DaySchedule]) -> str:
    start, end = period
    following = _same_line(next_day, line)
    tail = continues_into(line.slots, following.slots if following else None, period)
    if tail is not None:
        return f"🔴 {hhmm(start)} – {hhmm(tail)} ({_t(lang, 'tomorrow_suffix')})"
    return f"🔴 {hhmm(start)} – {hhmm(end)}"


def _same_line(day: Optional[DaySchedule], line: Line) -> Optional[Line]:
    if day is None:
        return None
    return next((l for l in day.lines if l.group == line.group), None)


def _day_body(lang: str, day: DaySchedule, from_minute: int, next_day: Optional[DaySchedule], empty_key: str) -> str:
    blocks = []
    multi = len(day.lines) > 1
    for i, line in enumerate(day.lines):
        periods = upcoming_periods(line.slots, from_minute)
        rows = [_period_text(lang, p, line, next_day) for p in periods] or [_t(lang, empty_key)]
        if multi:
            rows.insert(0, _line_header(lang, i, line))
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def render_schedule(event: ScheduleEvent, snapshot: Snapshot, now: datetime, lang: str) -> str:
    today = _today(now)
    is_today = event.day == today
    when = "today" if is_today else "tomorrow"
    label = event.day.strftime("%d.%m")
    from_minute = minute_of_day(now) if is_today else 0
    empty_key = "no_more_today" if is_today else "no_outages"
    next_day = snapshot.day(event.day + timedelta(days=1))

    if event.change is ScheduleChange.CLEARED or event.schedule is None:
        title = _t(lang, "today_changed" if is_today else "tomorrow_cleared")
        return f"📅 <b>{title}</b> ({label})\n\n{_t(lang, empty_key)}"

    key = f"{when}_{'published' if event.change is ScheduleChange.PUBLISHED else 'changed'}"
    body = _day_body(lang, event.schedule, from_minute, next_day, empty_key)
    return f"📅 <b>{_t(lang, key)}</b> ({label})\n\n{body}"


def next_outage_text(snapshot: Optional[Snapshot], now: datetime, lang: str) -> Optional[str]:
    """Next scheduled outage after `now`, per line; None if the schedule shows none."""
    if snapshot is None or snapshot.schedules_suspended:
        return None
    today = _today(now)
    today_day = snapshot.day(today)
    tomorrow_day = snapshot.day(today + timedelta(days=1))
    minute = minute_of_day(now)
    lines = today_day.lines if today_day else (tomorrow_day.lines if tomorrow_day else ())
    found = []
    for i, line in enumerate(lines):
        text = _next_for_line(lang, line, today_day, tomorrow_day, minute)
        if text and len(lines) > 1:
            text = f"{_t(lang, 'line')} {i + 1}: {text}"
        if text:
            found.append(text)
    return "; ".join(found) or None


def _next_for_line(lang: str, line: Line, today_day: Optional[DaySchedule],
                   tomorrow_day: Optional[DaySchedule], minute: int) -> Optional[str]:
    today_line = _same_line(today_day, line)
    tomorrow_line = _same_line(tomorrow_day, line)
    if today_line is not None:
        period = next_period(today_line.slots, minute)
        if period:
            return _period_text(lang, period, today_line, tomorrow_day).removeprefix("🔴 ")
    if tomorrow_line is not None:
        period = next_period(tomorrow_line.slots, -1)
        if period:
            return f"{_t(lang, 'tomorrow_suffix')} {hhmm(period[0])} – {hhmm(period[1])}"
    return None


# ---------------------------------------------------------------- outages

def _moment(raw: str, now: datetime, lang: str) -> str:
    parsed = parse_dtek_datetime(raw)
    return escape(format_moment(parsed, _today(now), lang) if parsed else raw)


def _outage_body(outage: Outage, now: datetime, lang: str) -> str:
    rows = []
    if outage.reason:
        rows.append(f"{_t(lang, 'reason')}: {escape(outage.reason)}")
    if outage.start:
        rows.append(f"{_t(lang, 'since')}: {_moment(outage.start, now, lang)}")
    if outage.end:
        rows.append(f"{_t(lang, 'restoration')}: <b>{_t(lang, 'until')} {_moment(outage.end, now, lang)}</b>")
    if outage.schedules_suspended:
        rows.append("")
        rows.append(_t(lang, "suspended"))
    return "\n".join(rows)


def render_outage(event: OutageEvent, snapshot: Snapshot, now: datetime, lang: str) -> str:
    if event.change is OutageChange.ENDED:
        text = f"✅ <b>{_t(lang, 'ended')}</b>"
        upcoming = next_outage_text(snapshot, now, lang)
        if upcoming:
            text += f"\n\n📅 {_t(lang, 'next_outage')}: <b>{upcoming}</b>"
        return text

    outage = event.outage
    assert outage is not None
    if event.change is OutageChange.RESTORATION_CHANGED and event.previous is not None:
        old = _moment(event.previous.end, now, lang) if event.previous.end else "—"
        new = _moment(outage.end, now, lang) if outage.end else "—"
        return (
            f"🕐 <b>{_t(lang, 'restoration_changed')}</b>\n\n"
            f"{old} → <b>{_t(lang, 'until')} {new}</b>\n\n{_outage_body(outage, now, lang)}"
        )

    icon = OUTAGE_ICONS[outage.kind]
    title = _t(lang, "updated") if event.change is OutageChange.UPDATED else _t(lang, outage.kind.value)
    return f"{icon} <b>{title}</b>\n\n{_outage_body(outage, now, lang)}"


# ---------------------------------------------------------------- power

def scheduled_end(snapshot: Optional[Snapshot], at: datetime, lang: str) -> Optional[str]:
    """End of the scheduled outage covering `at`, or None if `at` is outside the schedule."""
    if snapshot is None:
        return None
    today = _today(at)
    day = snapshot.day(today)
    if day is None:
        return None
    minute = minute_of_day(at)
    ends = []
    for line in day.lines:
        period = current_period(line.slots, minute)
        if period is None:
            continue
        following = _same_line(snapshot.day(today + timedelta(days=1)), line)
        tail = continues_into(line.slots, following.slots if following else None, period)
        ends.append(f"{_t(lang, 'tomorrow_suffix')} {hhmm(tail)}" if tail is not None else hhmm(period[1]))
    return " / ".join(dict.fromkeys(ends)) or None


def render_power(transition: PowerTransition, snapshot: Optional[Snapshot], now: datetime, lang: str) -> str:
    battery = escape(transition.battery)
    duration = format_duration(transition.duration_seconds, lang)
    at_text = transition.at.astimezone(KYIV_TZ).strftime("%H:%M")

    if transition.status is PowerStatus.ON:
        text = (
            f"🟢 <b>{_t(lang, 'power_on')}</b>\n\n"
            f"⏱ {_t(lang, 'duration')}: <b>{duration}</b>\n"
            f"🔋 {_t(lang, 'battery')}: <b>{battery}</b>"
        )
        upcoming = next_outage_text(snapshot, now, lang)
        if upcoming:
            text += f"\n\n📅 {_t(lang, 'next_outage')}: <b>{upcoming}</b>"
        if transition.detected_late:
            text += "\n\n" + _t(lang, "late_on").format(time=at_text)
        return text

    text = (
        f"🔴 <b>{_t(lang, 'power_off')}</b>\n\n"
        f"⏱ {_t(lang, 'uptime')}: <b>{duration}</b>\n"
        f"🔋 {_t(lang, 'battery')}: <b>{battery}</b>"
    )
    end = scheduled_end(snapshot, transition.at, lang)
    if end:
        text += f"\n\n{_t(lang, 'scheduled')}\n🕐 {_t(lang, 'restoration')}: <b>{end}</b>"
    else:
        text += f"\n\n{_t(lang, 'unscheduled')}"
    outage = snapshot.outage if snapshot else None
    if outage is not None:
        detail = escape(outage.reason) if outage.reason else _t(lang, outage.kind.value)
        if outage.end:
            detail += f", {_t(lang, 'until')} {_moment(outage.end, now, lang)}"
        text += f"\n{OUTAGE_ICONS[outage.kind]} {_t(lang, 'dtek')}: {detail}"
    if transition.detected_late:
        text += "\n\n" + _t(lang, "late_off").format(time=at_text)
    return text
