"""deye-reserve around the planner: inputs, messages, events, and the (normally off) write path."""
import copy
import logging
from datetime import date, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from deye_reserve.config import ConfigError, load_config
from deye_reserve.deye import parse_tou, tou_request
from deye_reserve.inputs import Forecast, InputError, changed_days, combine_forecast, parse_schedule, rank_models
from deye_reserve.messages import change_reasons, night_line, range_line, render_charge
from deye_reserve.net import Broadcast, HttpSources
from deye_reserve.planner import Params, Slot, safe_table
from deye_reserve.review import actual, decisions, sun_lines, with_mix
from deye_reserve.service import ReserveService
from deye_reserve.web import make_app
from deye_reserve.timeutil import KYIV_TZ
from tests.conftest import fixture

ENV = {
    "DEYE_APP_ID": "202511086802012",
    "DEYE_APP_SECRET": "0" * 32,
    "DEYE_EMAIL": "owner@example.com",
    "DEYE_PASSWORD_SHA256": "a" * 64,
    "DEYE_STATION_ID": "61918417",
    "DEYE_DEVICE_SN": "2511090098",
    "BOT_SCHEDULEBOT_TOKEN": "2:b",
    "DEYE_RESERVE_CHAT_ID": "370921204",
    # Most tests read the table messages; off, the default, is tested on its own.
    "DEYE_RESERVE_TABLE_MESSAGES": "true",
    # The Open-Meteo fixtures of 17.10 are horizontal radiation, with the factor the panels had for it then.
    "DEYE_RESERVE_PV_PER_GTI": "5.35",
}
SCHEDULE = fixture("krem_schedule_api_20261017.json")      # 17.10 15:23: power off 00-01 and 15-17, tomorrow unknown
MAX_AGE = timedelta(hours=3)


def T(day, hour, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=KYIV_TZ)


def with_tomorrow(payload, off_hours, day="2026-10-18"):
    payload = copy.deepcopy(payload)
    slots = ["power_off" if h in off_hours else "power_on" for h in range(24)]
    payload["days"].append({"date": day, "label": "", "is_today": False,
                            "lines": [{"group": "GPV2.2", "name": "Черга 2.2", "slots": slots}]})
    return payload


# ---- schedule ----

def test_schedule_from_dtek_service():
    s = parse_schedule(SCHEDULE, "GPV2.2", T(17, 15, 30), MAX_AGE)
    assert s.outages == ((T(17, 0), T(17, 1)), (T(17, 15), T(17, 17)))
    assert not s.tomorrow_published and s.group_found and s.updated == "17.10.2026 14:15"


def test_schedule_halves_and_midnight():
    payload = copy.deepcopy(SCHEDULE)
    slots = payload["days"][0]["lines"][0]["slots"]
    slots[15], slots[16], slots[20], slots[23] = "power_off_second_30", "power_off_first_30", "power_off", "power_off"
    s = parse_schedule(with_tomorrow(payload, {0, 1}), "GPV2.2", T(17, 15, 30), MAX_AGE)
    assert s.tomorrow_published
    assert s.outages == ((T(17, 0), T(17, 1)), (T(17, 15, 30), T(17, 16, 30)), (T(17, 20), T(17, 21)),
                         (T(17, 23), T(18, 2)))


def test_quiet_tomorrow_counts_as_not_published():
    s = parse_schedule(with_tomorrow(SCHEDULE, set()), "GPV2.2", T(17, 15, 30), MAX_AGE)
    assert not s.tomorrow_published and date(2026, 10, 18) not in s.days


def test_unusable_schedules():
    with pytest.raises(InputError, match="не оновлювався"):
        parse_schedule(SCHEDULE, "GPV2.2", T(17, 19), MAX_AGE)
    with pytest.raises(InputError, match="призупинив"):
        parse_schedule({**SCHEDULE, "schedules_suspended": True}, "GPV2.2", T(17, 15, 30), MAX_AGE)
    with pytest.raises(InputError, match="сьогоднішнього"):
        parse_schedule(SCHEDULE, "GPV2.2", T(18, 1), timedelta(days=1))


def test_another_group_takes_every_line():
    s = parse_schedule(SCHEDULE, "GPV1.1", T(17, 15, 30), MAX_AGE)
    assert not s.group_found and len(s.outages) == 2


def test_only_real_schedule_changes_count():
    today = date(2026, 10, 17)
    days = parse_schedule(SCHEDULE, "GPV2.2", T(17, 15, 30), MAX_AGE).days
    sent = {d.isoformat(): list(s) for d, s in days.items()}
    assert changed_days(None, days, today) == [today]
    assert changed_days(sent, days, today) == []
    later = parse_schedule(with_tomorrow(SCHEDULE, {9, 10}), "GPV2.2", T(17, 15, 30), MAX_AGE).days
    assert changed_days(sent, later, today) == [date(2026, 10, 18)]
    # Tomorrow's outages withdrawn is news; yesterday leaving at midnight is not.
    assert changed_days({d.isoformat(): list(s) for d, s in later.items()}, days, today) == [date(2026, 10, 18)]
    assert changed_days({"2026-10-16": ["power_off"] * 24, **sent}, days, today) == []


# ---- the service, with fakes at the edges ----

class Sources:
    def __init__(self, clock):
        self.payload = {k: v for k, v in copy.deepcopy(SCHEDULE).items() if k != "updated_at"}
        self.reading = {"soc": 82.0, "grid_present": True}      # taken a minute ago unless "at" is given
        self.weather_ok = True
        self._clock = clock

    async def schedule(self):
        # dtek-service fetches every minute; a test that wants it stale says so.
        return {"updated_at": (self._clock() - timedelta(minutes=1)).isoformat(), **self.payload}

    async def inverter(self):
        return {"source": "krem", "reading": {"at": (self._clock() - timedelta(minutes=1)).isoformat(), **self.reading}}

    async def previous_runs(self, first, last):
        return fixture("openmeteo_previous_day1_models_20261013_17.json")

    async def weather(self):
        if not self.weather_ok:
            raise RuntimeError("open-meteo down")
        return fixture("openmeteo_models_20261017_1511.json"), fixture("openmeteo_ensemble_20261017_1511.json")


class Deye:
    def __init__(self):
        self.tou = safe_table(Params())
        self.updates, self.statuses, self.calls = [], [], 0
        self.lag = 1             # reads that still show the old table after a write
        self.pending = None

    async def inverter_sn(self, station_id):
        return "2511090098"

    async def history_power(self, station_id, start, end):
        return fixture("deye_history_power_20261009_17.json")["stationDataItems"]

    async def read_tou(self, sn):
        self.calls += 1
        if self.pending is not None:
            if self.lag:
                self.lag -= 1
            else:
                self.tou, self.pending = self.pending, None
        return self.tou, []

    async def update_tou(self, request):
        self.updates.append(request)
        self.pending = tuple(Slot(int(i["time"][:2]) * 60 + int(i["time"][3:]), i["soc"], i["enableGridCharge"])
                             for i in request["timeUseSettingItems"])
        return "9001"

    async def order(self, order_id):
        return {"status": self.statuses.pop(0) if self.statuses else 666}


class Messenger:
    def __init__(self):
        self.texts = []

    async def send(self, text):
        self.texts.append(text)
        return True


@pytest.fixture
def make(tmp_path):
    def build(now=T(17, 15, 30), **env):
        clock = {"now": now}

        async def no_sleep(_):
            return None
        config = load_config({**ENV, "DEYE_RESERVE_DATA_DIR": str(tmp_path), **env})
        service = ReserveService(config, Sources(lambda: clock["now"]), Deye(), Messenger(),
                                 clock=lambda: clock["now"], sleep=no_sleep)
        return service, clock
    return build


async def ready(service, now):
    await service.refresh_history(now)
    await service.refresh_forecast(now)


async def test_plan_is_sent_once_and_again_when_the_schedule_changes(make):
    service, clock = make()
    await ready(service, clock["now"])
    report = await service.replan(clock["now"], "test")
    assert report.fallback is None and report.plan.safe
    # Only the final table, for the day whose schedule is news.
    assert len(service.messenger.texts) == 1 and service.messenger.texts[0].startswith(
        "🔋 <b>Таблиця TOU на сьогодні, 17.10</b>\n<pre>" + "\n".join(
            f"{s.time}  {s.soc:>3}%{'  ⚡ заряд від мережі' if s.grid_charge else ''}" for s in report.table)
        + "</pre>\nℹ️ Лише рекомендація: запис вимкнено\n\nПричина: опубліковано графік на 17.10"
        + "; ніч під 18.10: прогноз сонця ")
    clock["now"] += timedelta(minutes=30)
    await service.replan(clock["now"], "hourly")
    assert len(service.messenger.texts) == 1                         # the schedule did not change: quiet
    service.sources.payload = with_tomorrow(service.sources.payload, {9, 10, 11})
    clock["now"] += timedelta(minutes=5)
    await service.replan(clock["now"], "schedule")
    assert len(service.messenger.texts) == 2
    assert service.messenger.texts[1].startswith("🔋 <b>Таблиця TOU на завтра, 18.10</b>")
    kinds = [r["kind"] for r in service.journal.read()]
    assert kinds == ["replan", "message", "replan", "replan", "message"]
    last = [r for r in service.journal.read() if r["kind"] == "replan"][-1]
    assert last["tomorrow_published"] and last["night"] == report.plan.night or last["night"] >= 45
    assert set(last["candidates"]) >= {"45", "95"} and last["expected"]["min_by"]


async def test_no_forecast_means_the_safe_table(make):
    service, clock = make()
    service.sources.weather_ok = False
    await ready(service, clock["now"])
    report = await service.replan(clock["now"], "test")
    assert report.table == safe_table(service.p) and report.fallback == "немає прогнозу сонця"
    assert "⚠️ Безпечна таблиця 90/45: немає прогнозу сонця" in service.messenger.texts[0]


async def test_stale_inverter_reading_means_the_safe_table(make):
    service, clock = make()
    await ready(service, clock["now"])
    service.sources.reading = {**service.sources.reading, "at": "2026-10-17T15:00:00+03:00"}
    report = await service.replan(clock["now"], "test")
    assert report.fallback == "немає свіжих даних інвертора"


async def test_recommendations_never_write(make):
    service, clock = make()
    await ready(service, clock["now"])
    await service.replan(clock["now"], "test")
    assert not service.cfg.write and service.deye.updates == [] and service.deye.calls == 0


def test_write_request():
    table = (Slot(0, 80, True), Slot(420, 45, False), Slot(660, 45, False), Slot(1020, 61, True),
             Slot(1080, 45, False), Slot(1380, 80, True))
    assert tou_request("2511090098", table, 6000) == {
        "deviceSn": "2511090098",
        "timeUseSettingItems": [
            {"time": "00:00", "soc": 80, "power": 6000, "enableGridCharge": True, "enableGeneration": False},
            {"time": "07:00", "soc": 45, "power": 6000, "enableGridCharge": False, "enableGeneration": False},
            {"time": "11:00", "soc": 45, "power": 6000, "enableGridCharge": False, "enableGeneration": False},
            {"time": "17:00", "soc": 61, "power": 6000, "enableGridCharge": True, "enableGeneration": False},
            {"time": "18:00", "soc": 45, "power": 6000, "enableGridCharge": False, "enableGeneration": False},
            {"time": "23:00", "soc": 80, "power": 6000, "enableGridCharge": True, "enableGeneration": False},
        ],
        "timeoutSeconds": 30,
    }
    with pytest.raises(ValueError):
        tou_request("x", table[:5], 6000)
    with pytest.raises(ValueError):
        tou_request("x", (table[1],) + table[1:], 6000)
    # /config/tou answers "0700"; that reads back as the same table.
    items = [{"time": i["time"].replace(":", ""), "soc": i["soc"], "power": 6000, "enableGridCharge": i["enableGridCharge"],
              "enableGeneration": False} for i in tou_request("x", table, 6000)["timeUseSettingItems"]]
    assert parse_tou(items)[0] == table


class Slept:
    """A sleep that only notes how long the service would have waited."""
    def __init__(self):
        self.waits = []

    async def __call__(self, seconds):
        self.waits.append(seconds)


def with_errors(service, statuses, error):
    service.deye.statuses = list(statuses)
    original = service.deye.order

    async def order(order_id):
        result = await original(order_id)
        return {**result, "error": error} if result["status"] == 500 else result
    service.deye.order = order


async def test_status_666_is_written_and_the_cloud_is_not_read_back(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true")
    await ready(service, clock["now"])
    service.deye.statuses = [100, 300, 666]
    report = await service.replan(clock["now"], "test")
    assert len(service.deye.updates) == 1 and service.deye.updates[0]["deviceSn"] == "2511090098"
    assert service.deye.updates[0]["timeoutSeconds"] == 120
    # The table goes to the chat as soon as the inverter took it: a table there is a table in the inverter.
    assert len(service.messenger.texts) == 1
    assert "</pre>\n\nПричина: опубліковано графік на 17.10; ніч під 18.10: " in service.messenger.texts[0]
    assert service.state.written_table == [[s.start, s.soc, s.grid_charge] for s in report.table]
    assert service.state.writes_on(date(2026, 10, 17)) == 1
    clock["now"] += timedelta(minutes=7)
    await service.replan(clock["now"], "hourly")
    # The same plan: nothing sent, and /config/tou was never read.
    assert len(service.deye.updates) == 1 and service.deye.calls == 0 and len(service.messenger.texts) == 1
    rows = [(r["kind"], r.get("ok"), r.get("attempts")) for r in service.journal.read() if r["kind"] == "write"]
    assert rows == [("write", True, 1)]


async def test_a_failed_write_is_tried_three_more_times_5_min_apart(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true")
    service._sleep = slept = Slept()
    await ready(service, clock["now"])
    with_errors(service, [500, 500, 666], "540")
    await service.replan(clock["now"], "test")
    assert len(service.deye.updates) == 3 and [w for w in slept.waits if w == 300] == [300, 300]
    assert service.state.written_table is not None
    assert not any("🚨" in t for t in service.messenger.texts) and "</pre>\n\nПричина: " in service.messenger.texts[0]


async def test_four_failed_tries_send_the_table_and_the_failure(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true")
    service._sleep = slept = Slept()
    await ready(service, clock["now"])
    with_errors(service, [300, 500, 500, 500, 500], "530")
    await service.replan(clock["now"], "test")
    assert len(service.deye.updates) == 4 and [w for w in slept.waits if w == 300] == [300] * 3
    text = service.messenger.texts[-1]
    assert text.startswith("🔋 <b>Таблиця TOU на сьогодні, 17.10</b>\n<pre>00:00")
    assert ("🚨 Не вдалося записати: 4 спроби з паузою 5 хв, остання: команда 9001: статус 500, "
            "інвертор не виконав команду\n\nПричина: опубліковано графік на 17.10; ніч під 18.10: ") in text
    assert service.state.written_table is None
    assert service.write_blocker(service.report, clock["now"]).startswith("після помилки чекаю")
    # Later, with no schedule news, a failed table is an alarm of its own, with the table in a code block.
    with_errors(service, [500] * 4, "540")
    clock["now"] += timedelta(minutes=31)
    await service.replan(clock["now"], "hourly")
    alarm = service.messenger.texts[-1]
    assert alarm.startswith("🚨 <b>Запис TOU не вдався</b>\n4 спроби з паузою 5 хв, остання: ")
    assert "інвертор не відповів вчасно" in alarm and "Планувалось:\n<pre>00:00" in alarm
    assert len(service.deye.updates) == 8 and service.state.writes_on(date(2026, 10, 17)) == 2


async def test_writes_go_straight_in_even_during_an_outage(make):
    # 16:00, inside the 15:00-17:00 outage with the grid gone: the logger is on the battery and takes it.
    service, clock = make(now=T(17, 16), DEYE_RESERVE_WRITE="true")
    await ready(service, clock["now"])
    service.sources.reading = {**service.sources.reading, "grid_present": False}
    await service.replan(clock["now"], "test")
    assert len(service.deye.updates) == 1 and "</pre>\n\nПричина: " in service.messenger.texts[0]


async def test_write_guards(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true")
    await ready(service, clock["now"])
    service.sources.reading = {**service.sources.reading, "at": "2026-10-17T17:30:00+03:00"}     # logger silent
    service.deye.tou = safe_table(service.p, night=65)          # so the safe 90/45 would need writing
    await service.replan(clock["now"], "test")
    assert service.deye.updates == []
    assert ("⏸ Запишу, щойно буде можна: інвертор не на зв&#x27;язку"
            "\n\nПричина: немає даних для плану; ніч під 18.10") in service.messenger.texts[0]
    report = service.report
    report.reading_fresh = True
    assert service.write_blocker(report, T(17, 18)) is None
    service.state.writes = {"2026-10-17": 6}
    assert service.write_blocker(report, T(17, 18)) == "вже 6 записів сьогодні"
    assert service.write_blocker(report, T(18, 9)) is None
    off = load_config({**ENV, "DEYE_RESERVE_DATA_DIR": "/tmp/x"})
    assert not off.write


def test_events_wait_for_the_batch_to_settle(make):
    service, _ = make()
    assert service.on_event("schedule", T(17, 14, 15)) == T(17, 14, 17, 30)
    assert service.on_event("schedule", T(17, 14, 16, 40)) == T(17, 14, 19, 10)
    # Never more than 5 minutes after the first one.
    assert service.on_event("grid", T(17, 14, 18)) == T(17, 14, 20)


async def test_events_replan_once_when_due(make):
    service, clock = make(now=T(17, 14, 15))
    await ready(service, clock["now"])
    await service.tick(clock["now"])                                  # the start-up plan
    assert len(service.messenger.texts) == 1
    first = service.last_replan
    service.on_event("schedule", T(17, 14, 16))
    await service.tick(T(17, 14, 17))
    assert service.last_replan == first                               # still settling
    await service.tick(T(17, 14, 18, 30))
    assert service.last_replan == T(17, 14, 18, 30) and service.event_due is None


async def test_a_settling_batch_holds_the_hourly_replan(make):
    # 17.10: the hourly replan ran 4 s after a webhook and beat the batch it was waiting for.
    service, clock = make(now=T(17, 15, 33, 48))
    await ready(service, clock["now"])
    await service.tick(clock["now"])
    service.on_event("schedule", T(17, 16, 33, 44))
    await service.tick(T(17, 16, 33, 48))                             # the hour is up, but a batch is settling
    assert service.last_replan == T(17, 15, 33, 48)
    await service.tick(T(17, 16, 36, 14))
    assert service.last_replan == T(17, 16, 36, 14)


async def test_poll_catches_a_lost_webhook(make):
    service, clock = make()
    await service.poll_schedule(clock["now"])
    assert service.event_due is None and service.state.seen_stamp == "17.10.2026 14:15"
    service.sources.payload["schedule_updated"] = "17.10.2026 15:40"
    await service.poll_schedule(T(17, 15, 41))
    assert service.event_due == T(17, 15, 43, 30)


async def test_forecast_refresh_times(make):
    service, _ = make()
    assert service.next_forecast_time(T(17, 15)) == T(17, 22)
    assert service.next_forecast_time(T(17, 22, 1)) == T(18, 5, 30)


async def test_a_new_forecast_leaves_the_hours_gone_by_alone(make):
    service, _ = make()
    await service.refresh_forecast(T(17, 15, 30))
    fresh, today, tomorrow = service.forecast, date(2026, 10, 17), date(2026, 10, 18)
    # An earlier, sunnier run; the next one comes at 12:30 and rewrites only what is still ahead.
    service.forecast = older = Forecast({k: v * 2 for k, v in fresh.p50.items()},
                                        {k: v * 2 for k, v in fresh.pess.items()}, T(17, 5, 30), True)
    await service.refresh_forecast(T(17, 12, 30))
    f = service.forecast
    assert f.fetched_at == T(17, 12, 30) and fresh.p50[(today, 11)] > 0
    assert (f.p50[(today, 11)], f.pess[(today, 11)]) == (older.p50[(today, 11)], older.pess[(today, 11)])
    assert (f.p50[(today, 12)], f.pess[(today, 12)]) == (fresh.p50[(today, 12)], fresh.pess[(today, 12)])
    assert f.total_kwh(tomorrow) == fresh.total_kwh(tomorrow)
    # 22:00: the day's sun is over, its total stays as it was; tomorrow is new.
    await service.refresh_forecast(T(17, 22))
    assert service.forecast.total_kwh(today) == f.total_kwh(today)
    assert set(service.forecast.p50) == set(fresh.p50)               # nothing older piles up


async def test_http_endpoints(make):
    service, clock = make()
    async with TestClient(TestServer(make_app(service))) as http:
        assert (await http.get("/plan")).status == 404
        r = await http.post("/events/schedule", json={"detail": "17.10.2026 14:15"})
        assert r.status == 202 and (await r.json())["queued"] == "schedule"
        assert (await http.post("/events/nope")).status == 404
        await ready(service, clock["now"])
        await service.replan(clock["now"], "test")
        plan = await (await http.get("/plan")).json()
        assert plan["safe"] and len(plan["table"]) == 6
        health = await (await http.get("/health")).json()
        assert health["write"] is False and health["event_due"]


def test_config_defaults_and_checks():
    cfg = load_config({k: v for k, v in ENV.items() if k not in ("DEYE_RESERVE_PV_PER_GTI", "DEYE_RESERVE_TABLE_MESSAGES")})
    assert (cfg.write, cfg.port, cfg.host, cfg.group, cfg.chat_id) == (False, 9997, "127.0.0.1", "GPV2.2", "370921204")
    assert cfg.table_messages is False and load_config(ENV).table_messages is True
    assert (cfg.pv_per_gti, cfg.tilt, cfg.azimuth) == (4.4, 30.0, 0.0)
    assert (cfg.watch_s, cfg.alert_margin_min, cfg.alert_unscheduled_h) == (180.0, 30.0, 2.0)
    assert cfg.params == Params() and cfg.debounce_s == 150 and cfg.max_writes_per_day == 6
    assert [t.isoformat() for t in cfg.forecast_at] == ["05:30:00", "22:00:00"]
    assert load_config({**ENV, "DEYE_RESERVE_CHARGE_KW": "3.1", "DEYE_RESERVE_NIGHT_MAX": "90"}).params.charge_kw == 3.1
    with pytest.raises(ConfigError, match="DEYE_RESERVE_CHAT_ID"):
        load_config({k: v for k, v in ENV.items() if k != "DEYE_RESERVE_CHAT_ID"})
    with pytest.raises(ConfigError, match="SHUTDOWN_SOC"):
        load_config({**ENV, "DEYE_RESERVE_DAY_FLOOR": "15"})
    with pytest.raises(ConfigError, match="boolean"):
        load_config({**ENV, "DEYE_RESERVE_WRITE": "maybe"})


async def test_review_lines_up_decisions_with_the_day(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true")
    await ready(service, clock["now"])
    await service.replan(clock["now"], "події: schedule")
    lines = decisions(service.journal.read())
    replan = next(line for line in lines if " REPLAN " in line)
    assert replan.startswith("17.10 18:00 REPLAN (події: schedule) soc 82.0 grid True night")
    assert "outages 17.10 00:00-01:00, 17.10 15:00-17:00; tomorrow published False" in replan
    assert any(line.startswith("17.10 18:0") and " WRITE ok order 9001" in line for line in lines)
    assert any("MESSAGE day 2026-10-17 (written)" in line for line in lines)
    # 16.10 as Deye recorded it, with that day's real outages.
    history = fixture("deye_history_power_20261009_17.json")["stationDataItems"]
    day = actual(history, T(16, 7), T(17, 7), [(T(16, 9, 5), T(16, 15)), (T(16, 18, 23), T(16, 22, 27))], Params())
    assert day[0] == "SOC: start 32%, lowest 19% at 16.10 11:45, 23:00 90%, end 90%"
    assert day[1].startswith("grid bought: day ") and not day[1].startswith("grid bought: day 0.00 kWh, night 0.00")
    assert day[2].startswith("outage 16.10 09:05-15:00: SOC ") and day[3].startswith("outage 16.10 18:23-22:27")


TOP3 = ["icon_seamless", "meteofrance_seamless", "ecmwf_ifs025"]       # 186, 187 and 194 W off on 13-17.10
SUN = ["meteofrance_seamless", "icon_seamless", "ukmo_seamless"]


async def test_the_sun_is_the_median_of_the_sun_models(make):
    service, clock = make()
    await ready(service, clock["now"])
    # All models are still ranked against 13-17.10 (watched): hours near a full battery do not count.
    assert [s["model"] for s in service.ranking[:3]] == TOP3 and service.ranking[-1]["model"] == "cma_grapes_global"
    assert all(s["hours"] >= 30 for s in service.ranking)
    # ...but the sun is the plain median of the configured three, whatever the ranking says.
    assert service.forecast.models[date(2026, 10, 18)] == SUN
    multi = fixture("openmeteo_models_20261017_1511.json")["hourly"]
    i = multi["time"].index("2026-10-18T13:00")
    med = sorted(multi[f"shortwave_radiation_{m}"][i] for m in SUN)[1]
    ens = fixture("openmeteo_ensemble_20261017_1511.json")["hourly"]
    p10 = sorted(v[i] for k, v in ens.items() if k != "time")[5]
    assert service.forecast.p50[(date(2026, 10, 18), 12)] == pytest.approx(med * 5.35)
    assert service.forecast.pess[(date(2026, 10, 18), 12)] == pytest.approx(min(p10, med) * 5.35)
    await service.replan(clock["now"], "test")
    assert service.journal.read()[0]["models"]["2026-10-18"] == SUN
    assert service.health()["sun_models"] == SUN


def test_a_sun_model_missing_a_day_is_left_out():
    multi, ens = fixture("openmeteo_models_20261017_1511.json"), fixture("openmeteo_ensemble_20261017_1511.json")
    fc = combine_forecast(multi, ens, 5.35, T(17, 15), SUN)
    assert fc.models[date(2026, 10, 18)] == SUN
    assert fc.models[date(2026, 10, 19)] == ["meteofrance_seamless", "icon_seamless"]      # UKMO has gaps
    h = multi["hourly"]
    j = h["time"].index("2026-10-19T13:00")
    two = [h[f"shortwave_radiation_{m}"][j] for m in SUN[:2]]
    assert fc.p50[(date(2026, 10, 19), 12)] == pytest.approx(sum(two) / 2 * 5.35)
    assert len(combine_forecast(multi, ens, 5.35, T(17, 15)).models[date(2026, 10, 18)]) == 7   # no models given: all


async def test_a_cached_forecast_of_other_models_is_fetched_again(make):
    service, clock = make()
    await ready(service, clock["now"])
    assert service._forecast_fresh(clock["now"])
    service.forecast.models[date(2026, 10, 18)] = TOP3       # built by the ranking, before 24.10
    assert not service._forecast_fresh(clock["now"])


def test_ranking_skips_hours_with_a_full_battery():
    history = fixture("deye_history_power_20261009_17.json")["stationDataItems"]
    previous = fixture("openmeteo_previous_day1_models_20261013_17.json")
    sunny = rank_models(previous, history, 5.35, date(2026, 10, 14), date(2026, 10, 14), min_hours=1)
    full = {datetime.fromtimestamp(i["timeStamp"], KYIV_TZ).hour for i in history
            if datetime.fromtimestamp(i["timeStamp"], KYIV_TZ).date() == date(2026, 10, 14) and i["batterySOC"] >= 95}
    assert full and max(s.hours for s in sunny) <= 24 - len(full)


def test_review_puts_the_plans_sun_and_the_models_next_to_the_panels():
    history = fixture("deye_history_power_20261009_17.json")["stationDataItems"]
    previous = with_mix(fixture("openmeteo_previous_day1_models_20261013_17.json"), SUN)
    models = rank_models(previous, history, 5.35, date(2026, 10, 16), date(2026, 10, 16), min_hours=1)
    assert "mix" in {s.model for s in models}
    rows = [{"at": "2026-10-15T22:00:00+03:00", "kind": "replan", "forecast": {"2026-10-16": [2.4, 1.1]}},
            {"at": "2026-10-16T05:30:00+03:00", "kind": "replan", "forecast": {"2026-10-16": [2.1, 1.0]}}]
    lines = sun_lines(rows, history, date(2026, 10, 16), models)
    assert lines[0].startswith("panels 16.10: 1.7") and "hours usable" in lines[0]
    assert lines[1] == "plan's sun day-ahead (15.10 22:00): P50 2.4 / pessimistic 1.1 kWh"
    assert lines[2] == "plan's sun morning (16.10 05:30): P50 2.1 / pessimistic 1.0 kWh"
    assert lines[3].startswith("models' day-ahead, usable hours: ") and "mix " in lines[3]


# ---- the sun on the panels' plane ----

def test_the_sun_is_irradiance_on_the_panels_plane():
    multi, ens = fixture("openmeteo_gti_models_20261019.json"), fixture("openmeteo_gti_ensemble_20261019.json")
    top = ["ukmo_seamless", "ecmwf_ifs025", "meteofrance_seamless"]
    fc = combine_forecast(multi, ens, 4.4, T(19, 0), top)
    h = multi["hourly"]
    i = h["time"].index("2026-10-19T13:00")
    med = sorted(h[f"global_tilted_irradiance_{m}"][i] for m in top)[1]
    assert fc.ensemble and fc.models[date(2026, 10, 19)] == top
    assert fc.p50[(date(2026, 10, 19), 12)] == pytest.approx(med * 4.4)
    members = sorted(v[i] for k, v in ens["hourly"].items() if k != "time")
    assert fc.pess[(date(2026, 10, 19), 12)] == pytest.approx(min(members[5], med) * 4.4)
    # The models are ranked on the same plane, by name.
    history = fixture("deye_history_power_20261009_17.json")["stationDataItems"]
    scores = rank_models(fixture("openmeteo_gti_previous_day1_20261013_17.json"), history, 4.4,
                         date(2026, 10, 13), date(2026, 10, 17))
    assert len(scores) == 7 and {s.model for s in scores} >= {"ecmwf_ifs025", "icon_seamless", "cma_grapes_global"}


async def test_open_meteo_is_asked_for_the_roof():
    asked = []

    class Answer:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self, content_type=None):
            return {}

    class Session:
        def get(self, url, params=None):
            asked.append((url, params))
            return Answer()

    sources = HttpSources(load_config(ENV), Session())
    await sources.weather()
    await sources.previous_runs(date(2026, 10, 13), date(2026, 10, 17))
    assert [p["hourly"] for _, p in asked] == ["global_tilted_irradiance", "global_tilted_irradiance",
                                               "global_tilted_irradiance_previous_day1"]
    # The sun from the three sun models only; all seven are ranked.
    assert asked[0][1]["models"] == "meteofrance_seamless,icon_seamless,ukmo_seamless"
    assert len(asked[2][1]["models"].split(",")) == 7
    assert all((p["tilt"], p["azimuth"]) == (30.0, 0.0) for _, p in asked)


# ---- a table per day ----

async def plan_with_tomorrow(make, now, **env):
    """17.10 with tomorrow out 16:00-24:00 and the night target capped at 60%: tomorrow needs a top-up at 14:15."""
    service, clock = make(now=now, DEYE_RESERVE_NIGHT_MAX="60", **env)
    service.sources.payload = with_tomorrow(service.sources.payload, set(range(16, 24)))
    service.sources.reading = {"soc": 50.0, "grid_present": True}
    await ready(service, clock["now"])
    return service, clock, await service.replan(clock["now"], "test")


async def test_tomorrows_table_goes_in_when_it_changes_nothing_today(make):
    service, clock, report = await plan_with_tomorrow(make, T(17, 12), DEYE_RESERVE_WRITE="true")
    today, tomorrow = report.tables[date(2026, 10, 17)], report.tables[date(2026, 10, 18)]
    assert any(s.grid_charge and 600 < s.start < 960 for s in tomorrow) and not any(
        s.grid_charge and 420 <= s.start < 1380 for s in today)
    assert report.table == today and report.switch_at == T(17, 16)
    assert service.state.written_table == [[s.start, s.soc, s.grid_charge] for s in today]
    # The chat shows both days' own tables; tomorrow's says when it goes in.
    text = service.messenger.texts[0]
    assert text.startswith("🔋 <b>Таблиця TOU на сьогодні, 17.10</b>")
    assert "\n\n🔋 <b>Таблиця TOU на завтра, 18.10</b>\n<pre>" in text
    assert ("⏭ Застосую о 16:00\n\nПричина: опубліковано графік на 17.10; опубліковано графік на 18.10"
            "; ніч під 18.10: прогноз сонця ") in text
    assert service.next_due() <= T(17, 16)
    clock["now"] = T(17, 16)
    await service.tick(clock["now"])
    assert service.last_replan == T(17, 16) and service.journal.read()[-1]["reason"] == "таблиця на завтра"
    assert service.state.written_table == [[s.start, s.soc, s.grid_charge] for s in service.report.tables[date(2026, 10, 18)]]
    assert len(service.deye.updates) == 2 and len(service.messenger.texts) == 1     # the same tables: quiet


async def test_a_schedule_change_that_keeps_the_table_is_quiet(make):
    service, clock = make()
    await ready(service, clock["now"])
    await service.replan(clock["now"], "test")
    assert len(service.messenger.texts) == 1
    # DTEK moves the outage by half an hour; today's table stays as it was: nothing to say.
    service.sources.payload["days"][0]["lines"][0]["slots"][16] = "power_off_first_30"
    clock["now"] += timedelta(minutes=5)
    await service.replan(clock["now"], "schedule")
    assert len(service.messenger.texts) == 1
    assert service.state.sent_schedule["2026-10-17"][16] == "power_off_first_30"
    # Tomorrow's first schedule is news even if its table is what the plan had anyway.
    service.sources.payload = with_tomorrow(service.sources.payload, {9})
    clock["now"] += timedelta(minutes=5)
    await service.replan(clock["now"], "schedule")
    assert len(service.messenger.texts) == 2 and service.messenger.texts[1].startswith("🔋 <b>Таблиця TOU на завтра, 18.10")


async def test_a_short_loss_of_inputs_keeps_the_last_plan(make):
    service, clock, first = await plan_with_tomorrow(make, T(17, 12))
    service.sources.weather_ok = False
    service.forecast = None
    clock["now"] = T(17, 14)
    report = await service.replan(clock["now"], "hourly")
    assert report.fallback == "немає прогнозу сонця" and report.kept_from == T(17, 12)
    assert report.tables == first.tables and report.table == first.table
    clock["now"] = T(17, 18, 1)                                       # six hours on: the safe table
    report = await service.replan(clock["now"], "hourly")
    assert report.kept_from is None and report.table == safe_table(service.p)


# ---- the battery during an outage ----

async def test_a_battery_that_will_not_last_is_reported_once(make):
    # 15:10 in the 15:00-17:00 outage: 20% at ~900 W lasts about an hour, the grid is due at 17:00 (+30 min).
    service, clock = make(now=T(17, 15, 10))
    service.sources.reading = {"soc": 20.0, "grid_present": False, "load_w": 950.0, "pv_w": 50.0}
    await service.watch(clock["now"])
    text = service.messenger.texts[-1]
    assert text.startswith("🪫 <b>Батареї може не вистачити до ввімкнення світла</b>\nЗаряд 20%, навантаження ~900 Вт")
    assert "вистачить приблизно до 16:1" in text and "\nЗа графіком світло з'явиться о 17:00\n" in text
    assert not any(line.endswith(".") for line in text.splitlines())
    await service.watch(T(17, 15, 13))
    assert len(service.messenger.texts) == 1                          # once per outage
    assert [r["kind"] for r in service.journal.read()] == ["alert"]
    # Light load: no alarm.
    quiet, _ = make(now=T(17, 15, 10))
    quiet.sources.reading = {"soc": 60.0, "grid_present": False, "load_w": 300.0, "pv_w": 0.0}
    await quiet.watch(T(17, 15, 10))
    assert quiet.messenger.texts == []


async def test_an_outage_outside_the_schedule_is_watched_too(make):
    service, clock = make(now=T(17, 19))
    service.sources.reading = {"soc": 20.0, "grid_present": False, "load_w": 600.0, "pv_w": 0.0}
    await service.watch(clock["now"])
    assert service.messenger.texts[-1].endswith("Світла немає поза графіком\nВарто зменшити навантаження")
    service.sources.reading = {**service.sources.reading, "grid_present": True}
    await service.watch(T(17, 19, 3))
    assert service._off_since is None and len(service.messenger.texts) == 1


async def test_todays_sun_for_the_mini_app(make):
    service, clock = make()
    async with TestClient(TestServer(make_app(service))) as http:
        assert (await http.get("/sun")).status == 404                 # no forecast yet
        await service.refresh_forecast(clock["now"])
        body = await (await http.get("/sun")).json()
    assert body["date"] == "2026-10-17" and body["p50_kwh"] > body["low_kwh"] > 0
    assert body["p50_kwh"] == round(service.forecast.total_kwh(date(2026, 10, 17)), 2)


# ---- every change of a day's table goes to the chat, with its reason ----

def test_reasons_say_what_changed_since_the_last_message():
    today = date(2026, 10, 19)
    quiet, off = ["power_on"] * 24, ["power_on"] * 9 + ["power_off"] * 4 + ["power_on"] * 11
    then = {"at": "2026-10-18T21:31:00+03:00", "schedule": {"2026-10-19": off}, "forecast_at": "a",
            "sun": {"2026-10-19": [5.3, 2.2]}, "load_at": "x", "load_kwh": 8.4, "calibration": False,
            "soc_expected": {"2026-10-19T11": 80.0}}
    ctx = {**then, "hour": "2026-10-19T11"}
    assert change_reasons(then, ctx, today, 79.0) == ["перерахунок за поточним зарядом 79%"]
    assert change_reasons(then, ctx, today, 79.0, restarted=True) == ["перезапуск сервісу"]
    later = {**ctx, "schedule": {"2026-10-19": quiet[:9] + ["power_off"] * 5 + quiet[14:], "2026-10-20": off},
             "forecast_at": "b", "sun": {"2026-10-19": [3.1, 1.4]}, "load_at": "y", "load_kwh": 9.1,
             "calibration": True}
    assert change_reasons(then, later, today, 70.0, night_over=True, expected_soc=80.0) == [
        "змінився графік на 19.10", "опубліковано графік на 20.10", "калібрувальний заряд до 100%",
        "нічний заряд позаду: нічні слоти тепер на наступну ніч",
        "прогноз сонця на 19.10: 5.3 → 3.1 кВт·год (песимістично 2.2 → 1.4)",
        "споживання будинку: 8.4 → 9.1 кВт·год на добу", "заряд 70% замість очікуваних 80%"]
    # Withdrawn outages, a forecast that barely moved, the plan back after a fallback.
    assert change_reasons({**then, "fallback": "немає прогнозу сонця"},
                          {**ctx, "schedule": {"2026-10-19": quiet}, "forecast_at": "b",
                           "sun": {"2026-10-19": [5.2, 2.1]}}, today, 80.0) == [
        "дані для плану знову є", "скасовано відключення на 19.10"]
    assert change_reasons(then, {**ctx, "forecast_at": "b"}, today, 80.0) == ["оновився прогноз сонця"]
    assert change_reasons(then, {**ctx, "fallback": "немає прогнозу сонця"}, today, 80.0) == ["немає даних для плану"]
    assert change_reasons(then, ctx, today, 80.0, unpublished=date(2026, 10, 20)) == [
        "графік на 20.10 ще не опубліковано: план без відключень"]


def test_a_day_without_a_message_is_held_to_the_last_table_shown(make):
    service, _ = make()
    a, b, c = [[0, 90, True]], [[0, 60, True]], [[0, 50, True]]
    service.state.sent_tables = {"2026-10-15": a, "2026-10-16": b, "2026-10-18": c}
    assert service._last_shown(date(2026, 10, 17)) == b and service._last_shown(date(2026, 10, 15)) is None
    service._prune_sent(date(2026, 10, 17))
    assert service.state.sent_tables == {"2026-10-16": b, "2026-10-18": c}


async def test_a_table_changed_by_the_sun_is_sent_with_the_forecast(make):
    service, clock = make()
    await ready(service, clock["now"])
    await service.replan(clock["now"], "test")
    # As if the last message had another night and an older, sunnier forecast for tomorrow.
    sent = service.state.sent_tables["2026-10-17"]
    sent[0][1] = sent[-1][1] = 55 if sent[0][1] != 55 else 65
    service.state.told.update(forecast_at=T(17, 5, 30).isoformat(), sun={"2026-10-18": [9.9, 5.0]})
    clock["now"] += timedelta(minutes=30)
    await service.replan(clock["now"], "новий прогноз")
    text = service.messenger.texts[-1]
    assert len(service.messenger.texts) == 2 and text.startswith("🔋 <b>Таблиця TOU на сьогодні, 17.10</b>\n<pre>")
    assert "</pre>\nℹ️ Лише рекомендація: запис вимкнено\n\nПричина: прогноз сонця на 18.10: 9.9 → " in text
    row = [r for r in service.journal.read() if r["kind"] == "message"][-1]
    assert row["reasons"][0].startswith("прогноз сонця на 18.10: 9.9 → ")
    clock["now"] += timedelta(minutes=30)
    await service.replan(clock["now"], "hourly")
    assert len(service.messenger.texts) == 2                         # the same table again: quiet


async def test_the_night_gone_by_is_a_reason_of_its_own(make):
    service, clock = make(now=T(17, 7, 30))
    await ready(service, clock["now"])
    await service.replan(clock["now"], "test")
    # Last evening's message showed the night that has just passed; the plan now has the coming night's.
    sent = service.state.sent_tables["2026-10-17"]
    sent[0][1] = sent[-1][1] = 55 if sent[0][1] != 55 else 65
    service.state.told["at"] = T(16, 21, 31).isoformat()
    clock["now"] += timedelta(minutes=30)
    await service.replan(clock["now"], "hourly")
    assert len(service.messenger.texts) == 2
    assert ("\n\nПричина: нічний заряд позаду: нічні слоти тепер на наступну ніч; ніч під 18.10: прогноз сонця "
            in service.messenger.texts[-1])


def test_days_with_the_same_table_share_one():
    from deye_reserve.messages import Report, render_days
    same = safe_table(Params(), night=55)
    report = Report(T(19, 19, 50), same, tables={date(2026, 10, 19): same, date(2026, 10, 20): same})
    text = render_days([date(2026, 10, 19), date(2026, 10, 20)], date(2026, 10, 19), report, ("written", ""),
                       ["змінився графік на 20.10"])
    assert text.startswith("🔋 <b>Таблиця TOU на сьогодні і завтра, 19–20.10</b>\n<pre>00:00   55%")
    assert text.count("<pre>") == 1 and text.endswith("</pre>\n\nПричина: змінився графік на 20.10")
    # Different tables stay apart; a month boundary spells both months.
    other = safe_table(Params(), night=70)
    report.tables[date(2026, 10, 20)] = other
    assert render_days([date(2026, 10, 19), date(2026, 10, 20)], date(2026, 10, 19), report, ("written", "")).count("<pre>") == 2
    from deye_reserve.messages import day_title
    assert day_title(date(2026, 10, 31), date(2026, 10, 31), date(2026, 11, 1)) == "на сьогодні і завтра, 31.10–01.11"


async def test_the_reason_is_what_changed_since_the_replan_before(make):
    # 20.10 12:45: the sun filled the battery, the week's full charge was done and the night moved; the
    # schedule change of the evening before had left the table as it was, so it is not a reason.
    service, clock = make()
    await ready(service, clock["now"])
    await service.replan(clock["now"], "test")
    sent = service.state.sent_tables["2026-10-17"]
    sent[0][1] = sent[-1][1] = 55 if sent[0][1] != 55 else 65
    evening = ["power_off" if h == 20 else "power_on" for h in range(24)]
    service.state.told = {**service.state.told, "schedule": {"2026-10-17": evening}}
    service.state.plan_ctx = {**service.state.plan_ctx, "calibration": True}
    service.sources.reading = {**service.sources.reading, "soc": 100.0}
    clock["now"] += timedelta(hours=1)
    await service.replan(clock["now"], "hourly")
    reasons = [r for r in service.journal.read() if r["kind"] == "message"][-1]["reasons"]
    assert reasons[0] == "батарея зарядилась до 100%: калібрування вже не потрібне"
    assert reasons[-1].startswith("ніч під 18.10: прогноз сонця ") and not any("графік" in r for r in reasons)
    assert service.state.plan_ctx["at"] == clock["now"].isoformat()
    # Before 07:00 the night under way is today's.
    assert night_line({"sun": {"2026-10-17": [3.84, 1.6]}}, date(2026, 10, 17)) == \
        "ніч під 17.10: прогноз сонця 3.8 кВт·год (песимістично 1.6)"


# ---- grid charging: the rate it really reaches, and the first one with 80 A told once ----

async def test_grid_charging_is_sampled_and_the_first_one_told_once(make):
    service, clock = make(now=T(20, 23))
    assert service.charge_kw() == service.p.charge_kw == 4.3 and service.state.charge_report_due
    soc = 52.0
    for _ in range(8):                                               # 23:03 .. 23:24, every 3 min
        clock["now"] += timedelta(minutes=3)
        service.sources.reading = {"soc": soc, "grid_present": True, "battery_w": 4150.0, "grid_w": 4440.0,
                                   "load_w": 290.0, "pv_w": 0.0, "at": (clock["now"] - timedelta(seconds=30)).isoformat()}
        await service.watch(clock["now"])
        soc += 1.2
    assert len(service.messenger.texts) == 1 and not service.state.charge_report_due
    text = service.messenger.texts[0]
    assert text.startswith("⚡ <b>Заряд від мережі з новими налаштуваннями</b>\nGrid Charge 80 А, Peak Shaving 4.5 кВт; 23:02–23:2")
    assert "Імпорт із мережі: 4.44 кВт у середньому, найбільше 4.44" in text and "Заряд батареї: 4.15 кВт (52 → " in text
    assert text.endswith("✅ Peak Shaving тримає імпорт у межах 4.5 кВт")
    assert len([r for r in service.journal.read() if r["kind"] == "charge"]) == 8
    # Told once: the rest of the night is quiet, and from 10 samples the plan takes the rate measured.
    for _ in range(4):
        clock["now"] += timedelta(minutes=3)
        service.sources.reading = {**service.sources.reading, "at": (clock["now"] - timedelta(seconds=30)).isoformat()}
        await service.watch(clock["now"])
    assert len(service.messenger.texts) == 1 and service.charge_kw() == 4.15
    # No grid, or sun on the panels: not a grid charge.
    service.sources.reading = {**service.sources.reading, "grid_present": False, "at": clock["now"].isoformat()}
    await service._watch_charge(service.sources.reading, clock["now"], clock["now"])
    assert len(service.state.charge_samples) == 12


def test_a_charge_capped_below_80_a_points_at_the_bms():
    samples = [["2026-10-21T23:0%d:00+03:00" % i, 2700.0, 2950.0, 250.0, 50.0 + i] for i in range(4)]
    text = render_charge(samples, Params())
    assert "Заряд батареї: 2.70 кВт (50 → 53%)" in text and "✅ Peak Shaving" in text
    assert text.endswith("схоже, обмежує BMS батареї")
    over = render_charge([[s[0], 4300.0, 4800.0, 500.0, s[4]] for s in samples], Params())
    assert "⚠️ Імпорт доходив до 4.80 кВт — більше за 4.5" in over and "BMS" not in over


# ---- table messages off (the default): the mini app shows the tables, the chat only what is urgent ----

async def test_with_table_messages_off_the_news_is_kept_for_the_mini_app(make):
    service, clock = make(DEYE_RESERVE_TABLE_MESSAGES="false")
    await ready(service, clock["now"])
    report = await service.replan(clock["now"], "test")
    assert service.messenger.texts == []
    rows = [r for r in service.journal.read() if r["kind"] == "message"]
    assert table_news(rows) == [("2026-10-17", False)]
    assert rows[0]["reasons"][0] == "опубліковано графік на 17.10"
    assert service.state.why["2026-10-17"] == {"at": clock["now"].isoformat(), "reasons": rows[0]["reasons"]}
    assert service.state.sent_tables["2026-10-17"] == [[s.start, s.soc, s.grid_charge] for s in report.table]
    body = service.plan_payload()
    assert body["why"] == service.state.why and body["write"] is False and body["written_table"] is None
    assert any(" TABLE day 2026-10-17 (off, not sent): опубліковано графік на 17.10" in line
               for line in decisions(service.journal.read()))
    # Tomorrow published: news again, still not in the chat.
    service.sources.payload = with_tomorrow(service.sources.payload, {9, 10, 11})
    clock["now"] += timedelta(minutes=5)
    await service.replan(clock["now"], "schedule")
    assert service.messenger.texts == [] and service.state.why["2026-10-18"]["reasons"][0] == "опубліковано графік на 18.10"
    assert table_news(service.journal.read()) == [("2026-10-17", False), ("2026-10-18", False)]


def table_news(rows):
    return [(r["day"], r["sent"]) for r in rows if r["kind"] == "message"]


async def test_with_table_messages_off_a_written_table_is_quiet_and_shown_as_written(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true", DEYE_RESERVE_TABLE_MESSAGES="false")
    await ready(service, clock["now"])
    report = await service.replan(clock["now"], "test")
    assert len(service.deye.updates) == 1 and service.messenger.texts == []
    body = service.plan_payload()
    assert body["write"] is True and body["last_write"]["ok"] is True
    assert body["written_table"] == [{"start": s.start, "soc": s.soc, "grid_charge": s.grid_charge} for s in report.table]


async def test_with_table_messages_off_a_failed_write_is_still_an_alarm(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true", DEYE_RESERVE_TABLE_MESSAGES="false")
    service._sleep = Slept()
    await ready(service, clock["now"])
    with_errors(service, [500] * 4, "540")
    await service.replan(clock["now"], "test")
    # The table news went to the mini app; the failure, which a table message would have carried, is an alarm.
    assert len(service.messenger.texts) == 1
    assert service.messenger.texts[0].startswith("🚨 <b>Запис TOU не вдався</b>\n4 спроби з паузою 5 хв, остання: ")
    assert "Планувалось:\n<pre>00:00" in service.messenger.texts[0]
    assert service.plan_payload()["last_write"]["ok"] is False and service.state.why["2026-10-17"]


async def test_with_table_messages_off_the_battery_alert_still_goes(make):
    service, clock = make(now=T(17, 15, 10), DEYE_RESERVE_TABLE_MESSAGES="false")
    service.sources.reading = {"soc": 20.0, "grid_present": False, "load_w": 950.0, "pv_w": 50.0}
    await service.watch(clock["now"])
    assert len(service.messenger.texts) == 1 and service.messenger.texts[0].startswith("🪫 <b>Батареї може не вистачити")


async def test_the_reasons_come_back_from_the_journal_when_the_state_has_none(make):
    service, clock = make(DEYE_RESERVE_TABLE_MESSAGES="false")
    await ready(service, clock["now"])
    await service.replan(clock["now"], "test")
    why = dict(service.state.why)
    service.state.why = {}
    service._save()
    restarted, _ = make(now=clock["now"] + timedelta(minutes=1))
    assert restarted.state.why == why and why["2026-10-17"]["reasons"][0] == "опубліковано графік на 17.10"
    assert make(now=T(18, 9))[0].state.why == {}                      # a day gone by stays out



# ---- every day's table news goes to the log, chat or not ----

def test_a_table_in_the_log_reads_as_ranges():
    night = (Slot(0, 70, True), Slot(420, 45, False), Slot(660, 45, False), Slot(900, 45, False),
             Slot(1140, 45, False), Slot(1380, 70, True))
    assert range_line(night) == "07–23 45% · 23–07 70% ⚡"
    top_up = (Slot(0, 75, True), Slot(420, 45, False), Slot(855, 61, True), Slot(915, 45, False),
              Slot(1140, 45, False), Slot(1380, 70, True))
    assert range_line(top_up) == "00:00–07:00 75% ⚡ · 07:00–14:15 45% · 14:15–15:15 61% ⚡ · 15:15–23:00 45% · " \
                                 "23:00–24:00 70% ⚡"
    assert range_line(tuple(Slot(m, 45, False) for m in (0, 240, 480, 720, 960, 1200))) == "00–24 45%"


def news_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("table for ")]


async def test_table_news_is_logged_with_messages_off(make, caplog):
    caplog.set_level(logging.INFO, logger="deye_reserve.service")
    service, clock = make(DEYE_RESERVE_TABLE_MESSAGES="false")
    await ready(service, clock["now"])
    report = await service.replan(clock["now"], "test")
    assert service.messenger.texts == []
    [line] = news_lines(caplog)
    assert line.startswith(f"table for 17.10: {range_line(report.table)}; a recommendation, writing is off; "
                           "Причина: опубліковано графік на 17.10; ніч під 18.10: прогноз сонця ")
    assert line.endswith("; chat: off")
    # Nothing new: nothing logged.
    clock["now"] += timedelta(minutes=30)
    await service.replan(clock["now"], "hourly")
    assert len(news_lines(caplog)) == 1


async def test_table_news_is_logged_with_messages_on_too(make, caplog):
    caplog.set_level(logging.INFO, logger="deye_reserve.service")
    service, clock, report = await plan_with_tomorrow(make, T(17, 12), DEYE_RESERVE_WRITE="true")
    assert len(service.messenger.texts) == 1
    today, tomorrow = news_lines(caplog)
    assert today.startswith(f"table for 17.10: {range_line(report.tables[date(2026, 10, 17)])}; in the inverter; "
                            "Причина: опубліковано графік на 17.10; опубліковано графік на 18.10")
    assert tomorrow.startswith(f"table for 18.10: {range_line(report.tables[date(2026, 10, 18)])}; "
                               "takes over at 16:00; Причина: ")
    assert today.endswith("; chat: sent") and tomorrow.endswith("; chat: sent")



# ---- the battery alert goes to every krem chat, and the mini app shows the time left after it ----

def test_the_battery_alert_goes_to_the_owner_and_every_krem_chat():
    assert load_config(ENV).alert_chat_ids == ("370921204",)
    krem = {**ENV, "KREM_CHAT_IDS": "222, 370921204,333"}
    assert load_config(krem).alert_chat_ids == ("370921204", "222", "333")
    assert load_config({**krem, "DEYE_RESERVE_ALERT_CHAT_IDS": "444"}).alert_chat_ids == ("370921204", "444")


async def test_a_broadcast_is_delivered_when_any_chat_took_it():
    class Refusing(Messenger):
        async def send(self, text):
            self.texts.append(text)
            return False
    took, gone = Messenger(), Refusing()
    assert await Broadcast([gone, took]).send("🪫") and took.texts == gone.texts == ["🪫"]
    assert not await Broadcast([Refusing()]).send("🪫")


async def test_after_the_alert_the_time_left_is_kept_until_the_grid_is_back(make):
    service, clock = make(now=T(17, 15, 10))
    service.alerts = everyone = Messenger()
    service.sources.reading = {"soc": 60.0, "grid_present": False, "load_w": 300.0, "pv_w": 0.0}
    await service.watch(clock["now"])
    assert everyone.texts == [] and service.battery_payload() is None        # it lasts: nothing to show
    # The house draws more: the alert goes to every krem chat, not the owner's chat alone.
    service.sources.reading = {"soc": 20.0, "grid_present": False, "load_w": 950.0, "pv_w": 50.0}
    service._readings = []
    clock["now"] = T(17, 15, 13)
    await service.watch(clock["now"])
    assert len(everyone.texts) == 1 and service.messenger.texts == []
    left = service.battery_payload()
    # 20% down to the 10% shutdown: 1.02 kWh x 0.95 at 900 W is about 65 min.
    assert (left["soc"], left["load_w"], left["back_at"]) == (20.0, 900, T(17, 17).isoformat())
    assert datetime.fromisoformat(left["empty_at"]) == T(17, 15, 12) + timedelta(hours=0.1 * 10.24 * 0.95 / 0.9)
    # Later it follows the load; the grid back clears it.
    service.sources.reading = {**service.sources.reading, "soc": 18.0}
    await service.watch(T(17, 15, 16))
    assert service.battery_payload()["soc"] == 18.0 and len(everyone.texts) == 1
    service.sources.reading = {**service.sources.reading, "grid_present": True}
    await service.watch(T(17, 15, 19))
    assert service.battery_payload() is None
    async with TestClient(TestServer(make_app(service))) as http:
        assert (await http.get("/battery")).status == 404
        service.battery = {"at": "x"}
        assert (await (await http.get("/battery")).json()) == {"at": "x"}
