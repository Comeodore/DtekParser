"""The deye-reserve loop: gathers inputs, replans, tells the owner, and (only if enabled) writes the inverter.

Replans happen
- 2.5 min after a webhook from dtek-service (krem schedule updated, grid
  lost or back): DTEK publishes in batches, so later events in the window
  push it on, but never past 5 min after the first;
- when the fallback poll of dtek-service's schedule (every 10 min) sees a
  new DTEK "last updated" stamp the webhook did not deliver;
- every hour, and after each forecast refresh (05:30 and 22:00).

A message goes out when the table, the plan's status or the schedule changes;
a replan that changes nothing stays quiet.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Optional, Protocol

from deye_reserve.config import Config
from deye_reserve.deye import ORDER_DONE, ORDER_ERRORS, ORDER_FINAL, DeyeError, DeyeRateLimited, tou_request
from deye_reserve.inputs import (Forecast, InputError, LoadProfile, Schedule, combine_forecast, load_profile,
                                 parse_schedule, schedule_changed)
from deye_reserve.messages import Report, render_plan, render_write, table_line
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
        self._tasks: set[asyncio.Task] = set()

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
        if self.event_due is not None and now >= self.event_due:
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
        self.state.previous_table = table_to_list(report.table)
        self.report, self.last_replan = report, now
        await self._tell(report, schedule, now)
        if self.cfg.write:
            await self._maybe_write(report, now)
        self._save()
        return report

    def _status(self, report: Report) -> str:
        if report.fallback:
            return "fallback"
        return "plan" if report.plan.safe else "unsafe"

    async def _tell(self, report: Report, schedule: Optional[Schedule], now: datetime) -> None:
        today = local(now).date()
        changed = (table_to_list(report.table) != self.state.sent_table or self._status(report) != self.state.sent_status
                   or (schedule is not None and schedule_changed(self.state.sent_schedule, schedule.days, today)))
        if not changed:
            logger.info(f"plan unchanged: {table_line(report.table)}")
            return
        report.inverter_table = await self.inverter_table(now)
        if not self.cfg.write:
            report.write_note = "ℹ️ Лише рекомендація: запис в інвертор вимкнено."
        elif report.inverter_table is not None and tuple(report.inverter_table) == tuple(report.table):
            report.write_note = ""
        else:
            blocker = self.write_blocker(report, now)
            report.write_note = "✍️ Записую в інвертор…" if blocker is None else f"⏸ Поки не записую: {blocker}."
        logger.info(f"plan changed: {table_line(report.table)} ({self._status(report)})")
        if await self.messenger.send(render_plan(report, self.p)):
            self.state.sent_table = table_to_list(report.table)
            self.state.sent_status = self._status(report)
            if schedule is not None:
                self.state.sent_schedule = {d.isoformat(): list(s) for d, s in schedule.days.items()}

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

    async def _maybe_write(self, report: Report, now: datetime) -> None:
        if table_to_list(report.table) == self.state.written_table:
            return
        current = await self.inverter_table(now)
        if current is not None and tuple(current) == tuple(report.table):
            self.state.written_table = table_to_list(report.table)
            return
        blocker = self.write_blocker(report, now)
        if blocker is not None:
            logger.info(f"not writing {table_line(report.table)}: {blocker}")
            return
        self.state.count_write(local(now).date())
        task = asyncio.create_task(self.write(report.table))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def write(self, table: Table) -> bool:
        """One command: send, wait for the inverter, then for the cloud to show the new table."""
        async with self._write_lock:
            ok, detail = False, ""
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
            self._save()
            await self.messenger.send(render_write(ok, detail, table))
            return ok

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
