"""What the grid gave today by tariff: the night (23:00-07:00) and the day (07:00-23:00).

Deye's DailyEnergyPurchased counts from the inverter's midnight, so until
07:00 all of it is night. Its value at 07:00 and at 23:00 (the last reading
before each) is noted here, at no extra Deye call, and saved to a file so a
restart keeps it: the night is the 07:00 value plus what came after 23:00,
the day the rest. A boundary not seen (the service down across it, or
readings too far apart) leaves the day unsplit, and the mini app shows the
total alone rather than a guess.

The marks also go to the reserve on Cloudflare with each push (payload), and
its stand-in page splits the day with them and with the ones it notes itself
while this service is away (worker.js followGridDay; merged with add).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Optional

from app.timeutil import KYIV_TZ

if TYPE_CHECKING:
    from app.inverter.deye import InverterReading

logger = logging.getLogger(__name__)


class GridDay:
    # Readings further apart than this across a boundary leave the counter there unknown.
    MAX_GAP = timedelta(minutes=10)

    def __init__(self, night_start_h: int, night_end_h: int, path: Optional[str] = None):
        self.night_start_h = night_start_h
        self.night_end_h = night_end_h
        self._path = path
        self._day: Optional[date] = None
        self._marks: dict[int, float] = {}      # hour → the counter then, on self._day

    def restore(self) -> None:
        if self._path is None or not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                saved = json.load(f)
            self._day = date.fromisoformat(saved["day"])
            self._marks = {int(h): float(v) for h, v in saved["marks"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            logger.warning(f"Grid day split not restored from {self._path}: {type(e).__name__}: {e}")

    def follow(self, before: Optional[InverterReading], reading: InverterReading) -> None:
        """Note the counter at a tariff boundary that lies between two readings in a row."""
        if before is None or before.grid_today_kwh is None or reading.grid_today_kwh is None:
            return
        a, b = before.at.astimezone(KYIV_TZ), reading.at.astimezone(KYIV_TZ)
        if a.date() != b.date() or b - a > self.MAX_GAP:
            return
        for hour in (self.night_end_h, self.night_start_h):
            if a < datetime.combine(b.date(), time(hour), KYIV_TZ) <= b:
                if self._day != b.date():
                    self._day, self._marks = b.date(), {}
                self._marks[hour] = before.grid_today_kwh
                logger.info(f"Grid today at {hour:02d}:00: {before.grid_today_kwh} kWh")
                self._save()

    def payload(self) -> dict:
        """The marks as the reserve keeps them: their day (None before the first), the counter at each tariff hour
        seen (hour as text → kWh) and the tariff's hours."""
        return {"day": self._day.isoformat() if self._day else None,
                "marks": {str(h): v for h, v in self._marks.items()},
                "night_start_h": self.night_start_h, "night_end_h": self.night_end_h}

    def add(self, day: Optional[str], marks: Optional[dict]) -> None:
        """Marks noted elsewhere (a payload's day and marks): a later day's replace these, the same day's are
        taken over these, an earlier day's are left out."""
        try:
            when = date.fromisoformat(day) if day else None
            noted = {int(h): float(v) for h, v in (marks or {}).items()}
        except (TypeError, ValueError, AttributeError):
            return
        if when is None or (self._day is not None and when < self._day):
            return
        if when != self._day:
            self._day, self._marks = when, {}
        self._marks.update(noted)

    def split(self, reading: InverterReading) -> Optional[tuple[float, float]]:
        """(night, day) kWh from the grid on the reading's day so far; None when that is not known."""
        total = reading.grid_today_kwh
        if total is None:
            return None
        t = reading.at.astimezone(KYIV_TZ)
        if t.hour < self.night_end_h:
            return total, 0.0
        marks = self._marks if self._day == t.date() else {}
        morning = marks.get(self.night_end_h)
        evening = total if t.hour < self.night_start_h else marks.get(self.night_start_h)
        # A counter that went back (the inverter's midnight off Kyiv's) makes no split at all.
        if morning is None or evening is None or not morning <= evening <= total:
            return None
        return round(morning + total - evening, 2), round(evening - morning, 2)

    def _save(self) -> None:
        if self._path is None:
            return
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"day": self._day.isoformat(), "marks": {str(h): v for h, v in self._marks.items()}}, f)
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning(f"Grid day split not saved to {self._path}: {e}")
