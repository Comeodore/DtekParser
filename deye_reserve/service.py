"""The deye-reserve loop: gathers inputs, replans, tells the owner, and (only if enabled) writes the inverter.

Replans happen
- 2.5 min after a webhook from dtek-service (krem schedule updated, grid
  lost or back): DTEK publishes in batches, so later events in the window
  push it on, but never past 5 min after the first;
- when the fallback poll of dtek-service's schedule (every 10 min) sees a
  new DTEK "last updated" stamp the webhook did not deliver;
- every hour, and after each forecast refresh (05:30 and 22:00).

With DEYE_RESERVE_WRITE=true each replan also gets the table into the
inverter when it differs. The owner hears only about a day whose DTEK
schedule was published or changed: that day's final table and whether it
is in the inverter; and about a write that failed. The reasons behind every
decision go to the journal (data/journal.jsonl), for the daily review.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Optional, Protocol

from deye_reserve.config import Config
from deye_reserve.deye import ORDER_DONE, ORDER_ERRORS, ORDER_FINAL, DeyeError, DeyeRateLimited, tou_request
from deye_reserve.inputs import (Forecast, InputError, LoadProfile, Schedule, changed_days, combine_forecast,
                                 load_profile, parse_schedule)
from deye_reserve.journal import Journal
from deye_reserve.messages import Report, render_alarm, render_day, table_line
from deye_reserve.planner import Inputs, Planner, Table, local, safe_table, window
from deye_reserve.state import load_state, read_json, save_state, table_from_list, table_to_list, write_json
from deye_reserve.timeutil import KYIV_TZ, now_kyiv

logger = logging.getLogger(__name__)


class Sources(Protocol):
    async def schedule(self) -> dict: ...                # dtek-service GET /api/v1/schedule (krem)
    async def inverter(self) -> dict: ...                # dtek-service GET /api/v1/inverter/latest
    async def weather(self) -> tuple[dict, Optional[dict]]: ...   # Open-Meteo models, ECMWF ensemble


class Deye(Protocol):
    async def inverter_sn(self, station_id: int) -> str: ...
    async def history_power(self, station_id: int, start: datetime, end: datetime) -> list[dict]: ...
    async def read_tou(self, device_sn: str) -> tuple[Table, list[dict]]: ...
    async def update_tou(self, request: dict) -> str: ...
    async def order(self, order_id: str) -> dict: ...


class Messenger(Protocol):
    async def send(self, text: str) -> bool: ...


class ReserveService:
    # The inverter reading must be this fresh to plan from (dtek-service polls every ~72 s).
    READING_MAX_AGE = timedelta(minutes=15)
    LOAD_DAYS = 7
    RETRY_INPUT = timedelta(minutes=15)
    RETRY_LOAD = timedelta(hours=1)
    TOU_CACHE = timedelta(minutes=10)
    # Writing: how long to wait for the inverter to take the command, then for the cloud to show it.
    ORDER_POLL_S = 5.0
    ORDER_WAIT_S = 90.0
    CONFIRM_EVERY_S = 60.0
    CONFIRM_TRIES = 6
    WRITE_BACKOFF = timedelta(minutes=30)

    def __init__(self, config: Config, sources: Sources, deye: Deye, messenger: Messenger,
                 clock=now_kyiv, sleep=asyncio.sleep):
        self.cfg = config
        self.p = config.params
        self.planner = Planner(config.params)
        self.sources = sources
        self.deye = deye
        self.messenger = messenger
        self._clock = clock
        self._sleep = sleep
        self._state_path = config.data_dir / "state.json"
        self.state = load_state(self._state_path)
        self.journal = Journal(config.data_dir / "journal.jsonl")
        self.journal.prune(clock())
        self.forecast: Optional[Forecast] = None
        self.load: Optional[LoadProfile] = None
        self._load_caches()
        self.report: Optional[Report] = None
        self.last_error = ""
        self.last_replan: Optional[datetime] = None
        self._wake = asyncio.Event()
        self._events: list[str] = []
        self._first_event: Optional[datetime] = None
        self.event_due: Optional[datetime] = None
        now = clock()
        self._next_poll = now
        self._next_replan = now
        self._next_forecast = now if not self._forecast_fresh(now) else self.next_forecast_time(now)
        self._next_load = now
        self._forecast_new = False
        self._tou_cache: Optional[tuple[datetime, Table]] = None
        self._write_lock = asyncio.Lock()
        self._write_blocked_until: Optional[datetime] = None

    # ---- caches ----

    def _load_caches(self) -> None:
        data = read_json(self.cfg.data_dir / "forecast.json")
        if data:
            try:
                self.forecast = Forecast.from_dict(data)
            except (KeyError, ValueError) as e:
                logger.warning(f"forecast cache unreadable: {e}")
        data = read_json(self.cfg.data_dir / "load.json")
        if data:
            try:
                self.load = LoadProfile.from_dict(data)
            except (KeyError, ValueError) as e:
                logger.warning(f"load cache unreadable: {e}")

    def _save(self) -> None:
        try:
            save_state(self._state_path, self.state)
        except OSError as e:
            logger.warning(f"state not saved: {e}")

    def _forecast_fresh(self, now: datetime) -> bool:
        today = local(now).date()
        return (self.forecast is not None and self.forecast.covers([today, today + timedelta(days=1)])
                and now - self.forecast.fetched_at < timedelta(hours=12))

    def next_forecast_time(self, now: datetime) -> datetime:
        lt = local(now)
        for days in (0, 1):
            for at in self.cfg.forecast_at:
                t = datetime.combine(lt.date() + timedelta(days=days), at, KYIV_TZ)
                if t > lt:
                    return t
        raise ValueError("DEYE_RESERVE_FORECAST_AT is empty")

    # ---- events ----

    def on_event(self, kind: str, now: Optional[datetime] = None) -> datetime:
        """A webhook or the fallback poll: replan once the batch has settled."""
        now = now or self._clock()
        if self._first_event is None:
            self._first_event = now
        self._events.append(kind)
        self.event_due = min(now + timedelta(seconds=self.cfg.debounce_s),
                             self._first_event + timedelta(seconds=self.cfg.max_wait_s))
        logger.info(f"event {kind}: replan at {local(self.event_due):%H:%M:%S}")
        self._wake.set()
        return self.event_due

    def next_due(self) -> datetime:
        times = [self._next_poll, self._next_replan, self._next_forecast, self._next_load]
        if self.event_due is not None:
            times.append(self.event_due)
        return min(times)

    async def run(self) -> None:
        while True:
            now = self._clock()
            try:
                await self.tick(now)
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.exception("tick failed")
                self._next_replan = min(self._next_replan, now + self.RETRY_INPUT)
            wait = (self.next_due() - self._clock()).total_seconds()
            if wait > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
            self._wake.clear()

    async def tick(self, now: datetime) -> None:
        if now >= self._next_forecast:
            await self.refresh_forecast(now)
        if now >= self._next_load:
            await self.refresh_load(now)
        if now >= self._next_poll:
            self._next_poll = now + timedelta(seconds=self.cfg.poll_s)
            await self.poll_schedule(now)
        reason = None
        if self.event_due is not None:
            # A batch is settling: the replan it brings covers the hourly one and a new forecast too.
            if now >= self.event_due:
                reason = "події: " + ", ".join(dict.fromkeys(self._events))
                self._events, self._first_event, self.event_due = [], None, None
        elif self._forecast_new:
            reason = "новий прогноз"
        elif now >= self._next_replan:
            reason = "плановий перерахунок"
        if reason:
            self._forecast_new = False
            self._next_replan = now + timedelta(seconds=self.cfg.replan_s)
            await self.replan(now, reason)

    # ---- inputs ----

    async def refresh_forecast(self, now: datetime) -> None:
        try:
            multi, ensemble = await self.sources.weather()
            self.forecast = combine_forecast(multi, ensemble, self.cfg.pv_per_ghi, now)
            write_json(self.cfg.data_dir / "forecast.json", self.forecast.to_dict())
            self._forecast_new = True
            self._next_forecast = self.next_forecast_time(now)
            logger.info(f"forecast refreshed (ensemble {'yes' if self.forecast.ensemble else 'no'})")
        except Exception as e:
            self._next_forecast = now + self.RETRY_INPUT
            logger.warning(f"forecast refresh failed: {type(e).__name__}: {e}")

    async def refresh_load(self, now: datetime) -> None:
        """The house's hourly load over the last week, from Deye history: one call a day."""
        if self.load is not None and local(self.load.fetched_at).date() == local(now).date():
            self._next_load = self._next_midnight(now)
            return
        try:
            start = now - timedelta(days=self.LOAD_DAYS)
            items = await self.deye.history_power(self.cfg.station_id, start, now)
            self.load = load_profile(items, local(start).date(), local(now).date(), now)
            write_json(self.cfg.data_dir / "load.json", self.load.to_dict())
            self._next_load = self._next_midnight(now)
            logger.info(f"load profile: {sum(self.load.mean.values()) / 1000:.1f} kWh/day over {self.load.days} days")
        except Exception as e:
            self._next_load = now + self.RETRY_LOAD
            logger.warning(f"load history failed: {type(e).__name__}: {e}")

    def _next_midnight(self, now: datetime) -> datetime:
        return datetime.combine(local(now).date() + timedelta(days=1), time(0, 20), KYIV_TZ)

    async def poll_schedule(self, now: datetime) -> None:
        """The fallback for a lost webhook: a new DTEK stamp means a new schedule."""
        try:
            stamp = (await self.sources.schedule()).get("schedule_updated") or None
        except Exception as e:
            logger.warning(f"schedule poll failed: {type(e).__name__}: {e}")
            return
        if stamp and self.state.seen_stamp and stamp != self.state.seen_stamp:
            self.on_event("schedule (опит)", now)
        if stamp:
            self.state.seen_stamp = stamp

    def calibrate(self, now: datetime) -> bool:
        """A full charge once in `calibration_days`, for the battery's SOC to stay true."""
        days = [d for d in (self.state.last_full, self.load.last_full.isoformat() if self.load and self.load.last_full else None) if d]
        if not days:
            return True
        _, end = window(now, self.p)
        return (local(end).date() - date.fromisoformat(max(days))).days >= self.cfg.calibration_days

    # ---- planning ----

    async def replan(self, now: datetime, reason: str) -> Report:
        logger.info(f"replanning ({reason})")
        report = Report(now, safe_table(self.p))
        schedule, reading = None, None
        try:
            payload = await self.sources.schedule()
            if payload.get("schedule_updated"):
                self.state.seen_stamp = payload["schedule_updated"]
            schedule = parse_schedule(payload, self.cfg.group, now, timedelta(hours=self.cfg.schedule_max_age_h))
            report.schedule = schedule
        except InputError as e:
            report.fallback = f"Немає графіка: {e}"
        except Exception as e:
            report.fallback = f"dtek-service не відповідає ({type(e).__name__})"
        try:
            reading = (await self.sources.inverter()).get("reading")
        except Exception as e:
            logger.warning(f"inverter reading failed: {type(e).__name__}: {e}")
        if reading:
            report.soc, report.grid_present = reading.get("soc"), reading.get("grid_present")
            if report.soc is not None and report.soc >= 99:
                self.state.last_full = local(now).date().isoformat()
        report.forecast = self.forecast
        start, end = window(now, self.p)
        days = sorted({local(start).date(), local(end).date()})
        if report.fallback is None:
            if not reading or reading.get("soc") is None or \
                    now - datetime.fromisoformat(reading["at"]) > self.READING_MAX_AGE:
                report.fallback = "немає свіжих даних інвертора"
            elif self.forecast is None or not self.forecast.covers(days):
                report.fallback = "немає прогнозу сонця"
            elif self.load is None:
                report.fallback = "немає профілю навантаження"
        if report.fallback is None:
            inputs = Inputs(now, float(reading["soc"]), schedule.outages, self.load.mean, self.load.p80,
                            self.forecast.p50, self.forecast.pess, self.calibrate(now))
            plan = self.planner.plan(inputs, table_from_list(self.state.previous_table))
            report.plan, report.table = plan, plan.table
            if local(end).date() > local(now).date() and not schedule.tomorrow_published:
                report.notes.append("Графік на завтра ще не опубліковано: план без відключень завтра, "
                                    "перерахую, щойно з'явиться")
            if not schedule.group_found:
                report.notes.append(f"У графіку немає черги {self.cfg.group}: беру всі лінії будинку")
        if report.grid_present is False and schedule is not None and \
                not any(a <= now < b for a, b in schedule.outages):
            report.notes.append("Мережі немає поза графіком")
        changed = table_to_list(report.table) != self.state.previous_table
        self.state.previous_table = table_to_list(report.table)
        self.report, self.last_replan = report, now
        logger.info(f"plan {'changed' if changed else 'unchanged'}: {table_line(report.table)}"
                    + (f" (fallback: {report.fallback})" if report.fallback else ""))
        outcome = await self._apply(report, now) if self.cfg.write else ("off", "")
        self._journal_replan(report, reason, outcome)
        await self._tell(report, schedule, now, outcome)
        self._save()
        return report

    async def _tell(self, report: Report, schedule: Optional[Schedule], now: datetime,
                    outcome: tuple[str, str]) -> None:
        """A day whose schedule was published or changed gets its final table; a failed write an alarm."""
        today = local(now).date()
        days = changed_days(self.state.sent_schedule, schedule.days, today) if schedule is not None else []
        if days:
            day = max(days)         # tomorrow's publication is the news when both changed
            if await self.messenger.send(render_day(day, today, report, outcome)):
                self.state.sent_schedule = {d.isoformat(): list(s) for d, s in schedule.days.items()}
                self.state.sent_table = table_to_list(report.table)
                self.journal.add("message", now, day=day.isoformat(), outcome=outcome[0])
        elif outcome[0] == "failed":
            await self.messenger.send(render_alarm(report.table, outcome[1]))
            self.journal.add("message", now, day=None, outcome="failed")

    def _journal_replan(self, report: Report, reason: str, outcome: tuple[str, str]) -> None:
        now = report.now
        today = local(now).date()
        row = {"reason": reason, "fallback": report.fallback, "soc": report.soc, "grid": report.grid_present,
               "table": table_to_list(report.table), "outcome": list(outcome), "notes": report.notes}
        if report.schedule is not None:
            row.update(outages=[[a.isoformat(), b.isoformat()] for a, b in report.schedule.outages],
                       tomorrow_published=report.schedule.tomorrow_published, stamp=report.schedule.updated)
        if self.forecast is not None:
            row["forecast"] = {d.isoformat(): [round(self.forecast.total_kwh(d), 2),
                                               round(self.forecast.total_kwh(d, True), 2)]
                               for d in (today, today + timedelta(days=1))}
            row["forecast_at"] = self.forecast.fetched_at.isoformat()
        if self.load is not None:
            row["load_kwh"] = [round(sum(self.load.mean.values()) / 1000, 2),
                               round(sum(max(v, self.load.mean.get(h, 0)) for h, v in self.load.p80.items())
                                     * self.p.safety_load_factor / 1000, 2)]
        if report.plan is not None:
            p = report.plan

            def sim(s):
                return {"buy_day": round(s.buy_day, 2), "buy_night": round(s.buy_night, 2),
                        "cost_adj": round(s.cost_adj, 2), "curtailed": round(s.curtailed, 2),
                        "soc_end": round(s.soc_end, 1), "unserved": round(s.unserved, 2),
                        "min_by": {p.outages[k][0].isoformat(): round(v, 1) for k, v in s.min_by.items()}}
            row.update(night=p.night, safe=p.safe, kept_previous=p.kept_previous, calibration=p.calibration,
                       window=[p.start.isoformat(), p.end.isoformat()], expected=sim(p.expected),
                       pessimistic=sim(p.pessimistic),
                       baseline_cost_adj=round(p.baseline.expected.cost_adj, 2) if p.baseline else None,
                       candidates={n: [s, round(c, 2), round(lo, 1)] for n, (s, c, lo) in p.candidates.items()},
                       soc_expected={local(t).strftime("%d.%m %H:%M"): round(v, 1)
                                     for t, v in p.expected.soc_at.items()})
        self.journal.add("replan", now, **row)

    async def device_sn(self) -> str:
        if self.cfg.device_sn:
            return self.cfg.device_sn
        if not self.state.device_sn:
            self.state.device_sn = await self.deye.inverter_sn(self.cfg.station_id)
        return self.state.device_sn

    async def inverter_table(self, now: datetime, fresh: bool = False) -> Optional[Table]:
        """The table in the inverter as Deye Cloud has it; None when it cannot be read."""
        if not fresh and self._tou_cache and now - self._tou_cache[0] < self.TOU_CACHE:
            return self._tou_cache[1]
        try:
            table, _ = await self.deye.read_tou(await self.device_sn())
        except Exception as e:
            logger.warning(f"TOU read failed: {type(e).__name__}: {e}")
            return None
        self._tou_cache = (now, table)
        return table

    # ---- writing (DEYE_RESERVE_WRITE=true only) ----

    def write_blocker(self, report: Report, now: datetime) -> Optional[str]:
        """Why the table must not be written now; None when it may."""
        if not self.cfg.write:
            return "запис вимкнено"
        if self._write_lock.locked():
            return "попередня команда ще виконується"
        if self._write_blocked_until and now < self._write_blocked_until:
            return f"після помилки чекаю до {local(self._write_blocked_until):%H:%M}"
        if report.grid_present is not True:
            return "немає мережі"
        if report.schedule is not None and any(a <= now < b for a, b in report.schedule.outages):
            return "триває відключення за графіком"
        if self.state.writes_on(local(now).date()) >= self.cfg.max_writes_per_day:
            return f"вже {self.cfg.max_writes_per_day} записів сьогодні"
        return None

    async def _apply(self, report: Report, now: datetime) -> tuple[str, str]:
        """Gets the table into the inverter: ("already" | "written" | "blocked" | "failed", detail)."""
        current = await self.inverter_table(now)
        if current is None and table_to_list(report.table) == self.state.written_table:
            return "already", ""
        if current is not None and tuple(current) == tuple(report.table):
            self.state.written_table = table_to_list(report.table)
            return "already", ""
        blocker = self.write_blocker(report, now)
        if blocker is not None:
            logger.info(f"not writing {table_line(report.table)}: {blocker}")
            return "blocked", blocker
        self.state.count_write(local(now).date())
        ok, detail = await self.write(report.table)
        return ("written", "") if ok else ("failed", detail)

    async def write(self, table: Table) -> tuple[bool, str]:
        """One command: send, wait for the inverter, then for the cloud to show the new table."""
        async with self._write_lock:
            ok, detail, order_id = False, "", None
            try:
                sn = await self.device_sn()
                order_id = await self.deye.update_tou(tou_request(sn, table, self.p.slot_power_w))
                logger.info(f"TOU write sent, order {order_id}: {table_line(table)}")
                result = await self._wait_order(order_id)
                status = _int(result.get("status")) if result else None
                if status != ORDER_DONE:
                    code = str((result or {}).get("error") or "")
                    detail = (f"команда {order_id}: статус {status}, {ORDER_ERRORS.get(code, code)}".rstrip(", ")
                              if result else f"інвертор не підтвердив команду {order_id} за {self.ORDER_WAIT_S:.0f} с")
                else:
                    ok, detail = await self._confirm(sn, table)
            except DeyeRateLimited:
                detail = "Deye Cloud: забагато запитів (429)"
            except (DeyeError, ValueError) as e:
                detail = str(e)
            except Exception as e:
                detail = f"{type(e).__name__}: {e}"
            now = self._clock()
            self.state.last_write = {"at": now.isoformat(), "table": table_to_list(table), "ok": ok, "detail": detail}
            if ok:
                self.state.written_table = table_to_list(table)
                self._write_blocked_until = None
                self._tou_cache = (now, table)
                logger.info("TOU write confirmed")
            else:
                self._write_blocked_until = now + self.WRITE_BACKOFF
                logger.error(f"TOU write failed: {detail}")
            self.journal.add("write", now, table=table_to_list(table), ok=ok, detail=detail, order=order_id)
            self._save()
            return ok, detail

    async def _wait_order(self, order_id: str) -> Optional[dict]:
        waited = 0.0
        while True:
            result = await self.deye.order(order_id)
            if _int(result.get("status")) in ORDER_FINAL:
                return result
            if waited >= self.ORDER_WAIT_S:
                return None
            await self._sleep(self.ORDER_POLL_S)
            waited += self.ORDER_POLL_S

    async def _confirm(self, sn: str, table: Table) -> tuple[bool, str]:
        """The cloud shows the new table 1-4 min after the inverter took it; look once a minute, up to 6 min."""
        current = None
        for attempt in range(self.CONFIRM_TRIES + 1):
            if attempt:
                await self._sleep(self.CONFIRM_EVERY_S)
            current, _ = await self.deye.read_tou(sn)
            if tuple(current) == tuple(table):
                return True, ""
        return False, f"через 6 хв у інверторі інша таблиця: {table_line(current)}"

    # ---- status ----

    def health(self) -> dict:
        now = self._clock()
        return {
            "status": "ok" if not self.last_error else "degraded",
            "last_error": self.last_error or None,
            "last_replan": self.last_replan.isoformat() if self.last_replan else None,
            "next_replan": self.next_due().isoformat(),
            "event_due": self.event_due.isoformat() if self.event_due else None,
            "forecast_at": self.forecast.fetched_at.isoformat() if self.forecast else None,
            "load_at": self.load.fetched_at.isoformat() if self.load else None,
            "write": self.cfg.write,
            "writes_today": self.state.writes_on(local(now).date()),
            "deye_calls": getattr(self.deye, "calls", None),
        }

    def plan_payload(self) -> Optional[dict]:
        r = self.report
        if r is None:
            return None
        body = {"at": r.now.isoformat(), "table": [s.__dict__ for s in r.table], "fallback": r.fallback,
                "soc": r.soc, "grid_present": r.grid_present, "notes": r.notes}
        if r.plan is not None:
            p = r.plan
            body.update(night=p.night, safe=p.safe, kept_previous=p.kept_previous, calibration=p.calibration,
                        window=[p.start.isoformat(), p.end.isoformat()],
                        outages=[[a.isoformat(), b.isoformat()] for a, b in p.outages],
                        expected={k: v for k, v in asdict(p.expected).items() if k not in ("soc_at",)},
                        pessimistic={k: v for k, v in asdict(p.pessimistic).items() if k not in ("soc_at",)},
                        soc_expected={local(t).strftime("%d.%m %H:%M"): round(v, 1) for t, v in p.expected.soc_at.items()},
                        candidates={n: {"safe": s, "cost": round(c, 2), "lowest": round(lo, 1)}
                                    for n, (s, c, lo) in p.candidates.items()})
        return body


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
