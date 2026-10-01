from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kyiv")
MINUTES_IN_DAY = 24 * 60


def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)


def minute_of_day(moment: datetime) -> int:
    local = moment.astimezone(KYIV_TZ)
    return local.hour * 60 + local.minute


def hhmm(minute: int) -> str:
    """Minute of day as HH:MM; the end of the day stays 24:00 so periods read naturally."""
    if minute >= MINUTES_IN_DAY:
        return "24:00"
    h, m = divmod(minute, 60)
    return f"{h:02d}:{m:02d}"


def parse_dtek_datetime(value: str) -> Optional[datetime]:
    """DTEK writes outage times as "HH:MM DD.MM.YYYY" in Kyiv time."""
    try:
        return datetime.strptime(value.strip(), "%H:%M %d.%m.%Y").replace(tzinfo=KYIV_TZ)
    except (ValueError, AttributeError):
        return None


def relative_day_label(moment: datetime, today: date, lang: str) -> str:
    """'' for today, 'tomorrow'/'завтра' for tomorrow, the date otherwise."""
    day = moment.astimezone(KYIV_TZ).date()
    if day == today:
        return ""
    if day == today + timedelta(days=1):
        return "tomorrow" if lang == "en" else "завтра"
    return day.strftime("%d.%m")


def format_moment(moment: datetime, today: date, lang: str) -> str:
    label = relative_day_label(moment, today, lang)
    time = moment.astimezone(KYIV_TZ).strftime("%H:%M")
    if not label:
        return time
    if label[0].isdigit():
        return f"{time} {label}"
    return f"{label} {time}"


def format_duration(seconds: float, lang: str) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    units = ("d", "h", "m") if lang == "en" else ("д", "г", "хв")
    parts = [f"{v}{u}" for v, u in zip((days, hours, minutes), units) if v]
    if parts:
        return " ".join(parts)
    return "less than a minute" if lang == "en" else "менше хвилини"
