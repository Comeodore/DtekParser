"""deye-reserve around the planner: inputs, messages, events, and the (normally off) write path."""
import copy
from datetime import date, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from deye_reserve.config import ConfigError, load_config
from deye_reserve.deye import parse_tou, tou_request
from deye_reserve.inputs import (InputError, changed_days, combine_forecast, parse_schedule, parse_solcast,
                                 rank_models)
from deye_reserve.net import HttpSources
from deye_reserve.planner import Params, Slot, safe_table
from deye_reserve.review import actual, decisions, solcast_lines
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
    assert service.messenger.texts == [
        "🔋 <b>Таблиця TOU на сьогодні, 17.10</b>\n<pre>" + "\n".join(
            f"{s.time}  {s.soc:>3}%{'  ⚡ заряд від мережі' if s.grid_charge else ''}" for s in report.table)
        + "</pre>\nℹ️ Лише рекомендація: запис вимкнено"]
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
    assert len(service.messenger.texts) == 1 and service.messenger.texts[0].endswith("</pre>")
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
    assert not any("🚨" in t for t in service.messenger.texts) and service.messenger.texts[0].endswith("</pre>")


async def test_four_failed_tries_send_the_table_and_the_failure(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true")
    service._sleep = slept = Slept()
    await ready(service, clock["now"])
    with_errors(service, [300, 500, 500, 500, 500], "530")
    await service.replan(clock["now"], "test")
    assert len(service.deye.updates) == 4 and [w for w in slept.waits if w == 300] == [300] * 3
    text = service.messenger.texts[-1]
    assert text.startswith("🔋 <b>Таблиця TOU на сьогодні, 17.10</b>\n<pre>00:00")
    assert text.endswith("🚨 Не вдалося записати: 4 спроби з паузою 5 хв, остання: команда 9001: статус 500, "
                         "інвертор не виконав команду")
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
    assert len(service.deye.updates) == 1 and service.messenger.texts[0].endswith("</pre>")


async def test_write_guards(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true")
    await ready(service, clock["now"])
    service.sources.reading = {**service.sources.reading, "at": "2026-10-17T17:30:00+03:00"}     # logger silent
    service.deye.tou = safe_table(service.p, night=65)          # so the safe 90/45 would need writing
    await service.replan(clock["now"], "test")
    assert service.deye.updates == []
    assert service.messenger.texts[0].endswith("⏸ Запишу, щойно буде можна: інвертор не на зв&#x27;язку")
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
    cfg = load_config({k: v for k, v in ENV.items() if k != "DEYE_RESERVE_PV_PER_GTI"})
    assert (cfg.write, cfg.port, cfg.host, cfg.group, cfg.chat_id) == (False, 9997, "127.0.0.1", "GPV2.2", "370921204")
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


async def test_the_sun_is_the_median_of_the_models_that_fit_the_panels(make):
    service, clock = make()
    await ready(service, clock["now"])
    # 13-17.10 against Deye's history: hours with the battery near full do not count (zero export cuts PV).
    assert [s["model"] for s in service.ranking[:3]] == TOP3 and service.ranking[-1]["model"] == "cma_grapes_global"
    assert all(s["hours"] >= 30 for s in service.ranking)
    assert service.forecast.models[date(2026, 10, 18)] == TOP3
    multi = fixture("openmeteo_models_20261017_1511.json")["hourly"]
    i = multi["time"].index("2026-10-18T13:00")
    top = sorted(multi[f"shortwave_radiation_{m}"][i] for m in TOP3)
    ens = fixture("openmeteo_ensemble_20261017_1511.json")["hourly"]
    p10 = sorted(v[i] for k, v in ens.items() if k != "time")[5]
    assert service.forecast.p50[(date(2026, 10, 18), 12)] == pytest.approx(top[1] * 5.35)
    assert service.forecast.pess[(date(2026, 10, 18), 12)] == pytest.approx(min(p10, top[1]) * 5.35)
    await service.replan(clock["now"], "test")
    assert service.journal.read()[0]["models"]["2026-10-18"] == TOP3


def test_a_model_missing_a_day_gives_way_to_the_next():
    multi, ens = fixture("openmeteo_models_20261017_1511.json"), fixture("openmeteo_ensemble_20261017_1511.json")
    ranking = ["ukmo_seamless", "ecmwf_ifs025", "gem_seamless", "icon_seamless"]
    fc = combine_forecast(multi, ens, 5.35, T(17, 15), ranking, 3)
    assert fc.models[date(2026, 10, 18)] == ["ukmo_seamless", "ecmwf_ifs025", "gem_seamless"]
    assert fc.models[date(2026, 10, 19)] == ["ecmwf_ifs025", "gem_seamless", "icon_seamless"]   # UKMO has gaps
    assert len(combine_forecast(multi, ens, 5.35, T(17, 15)).models[date(2026, 10, 18)]) == 7     # no ranking: all


def test_ranking_skips_hours_with_a_full_battery():
    history = fixture("deye_history_power_20261009_17.json")["stationDataItems"]
    previous = fixture("openmeteo_previous_day1_models_20261013_17.json")
    sunny = rank_models(previous, history, 5.35, date(2026, 10, 14), date(2026, 10, 14), min_hours=1)
    full = {datetime.fromtimestamp(i["timeStamp"], KYIV_TZ).hour for i in history
            if datetime.fromtimestamp(i["timeStamp"], KYIV_TZ).date() == date(2026, 10, 14) and i["batterySOC"] >= 95}
    assert full and max(s.hours for s in sunny) <= 24 - len(full)


def test_ecmwf_outside_the_top_has_no_say():
    # 03-17.10 on the server: Météo-France, ICON and UKMO fit best, ECMWF came fourth. Until 19.10 it still
    # had its own half of P50; against the panels on 09-18.10 that read more sun than came.
    multi, ens = fixture("openmeteo_models_20261017_1511.json"), fixture("openmeteo_ensemble_20261017_1511.json")
    top = ["meteofrance_seamless", "icon_seamless", "ukmo_seamless"]
    fc = combine_forecast(multi, ens, 5.35, T(17, 15), top + ["ecmwf_ifs025"], 3)
    h = multi["hourly"]
    i = h["time"].index("2026-10-18T13:00")
    med = sorted(h[f"shortwave_radiation_{m}"][i] for m in top)[1]
    assert fc.models[date(2026, 10, 18)] == top
    assert fc.p50[(date(2026, 10, 18), 12)] == pytest.approx(med * 5.35)
    assert fc.total_kwh(date(2026, 10, 18)) >= fc.total_kwh(date(2026, 10, 18), pessimistic=True)


SOLCAST = {"forecasts": [
    {"pv_estimate": 1.2, "pv_estimate10": 0.6, "pv_estimate90": 1.6, "period_end": "2026-10-18T07:30:00.0000000Z", "period": "PT30M"},
    {"pv_estimate": 1.4, "pv_estimate10": 0.8, "pv_estimate90": 1.8, "period_end": "2026-10-18T08:00:00.0000000Z", "period": "PT30M"},
    {"pv_estimate": 2.0, "pv_estimate10": 1.0, "pv_estimate90": 2.4, "period_end": "2026-10-19T08:30:00.0000000Z", "period": "PT30M"},
]}


def test_solcast_half_hours_become_kyiv_hours():
    hours = parse_solcast(SOLCAST)
    # 07:00-08:00 UTC is 10:00-11:00 in Kyiv (UTC+3 until 25.10)
    assert hours[(date(2026, 10, 18), 10)] == pytest.approx((1300, 700, 1700))
    assert hours[(date(2026, 10, 19), 11)] == pytest.approx((2000, 1000, 2400))


class FakeSolcast:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    async def forecasts(self):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else SOLCAST
        if isinstance(answer, Exception):
            raise answer
        return answer


async def test_solcast_is_watched_within_its_budget(tmp_path):
    clock = {"now": T(18, 5, 34)}
    config = load_config({**ENV, "DEYE_RESERVE_DATA_DIR": str(tmp_path), "SOLCAST_API_KEY": "k", "SOLCAST_SITE_ID": "s",
                          "DEYE_RESERVE_SOLCAST_MAX_CALLS": "3"})
    solcast = FakeSolcast(RuntimeError("busy"))
    service = ReserveService(config, Sources(lambda: clock["now"]), Deye(), Messenger(), clock=lambda: clock["now"],
                             solcast=solcast)
    assert service._next_solcast == T(18, 8, 34)
    await service.refresh_solcast(T(18, 8, 34))                      # a failure: once more in 15 min
    assert service._next_solcast == T(18, 8, 49) and service.solcast_log.read() == []
    await service.refresh_solcast(T(18, 8, 49))
    row = service.solcast_log.read()[0]
    assert row["days"]["2026-10-18"] == [1.3, 0.7, 1.7] and row["hours"]["2026-10-18T10"] == [1300, 700, 1700]
    assert service._next_solcast == T(18, 10, 34)
    await service.refresh_solcast(T(18, 10, 34))
    await service.refresh_solcast(T(18, 12, 34))                     # the fourth call today: over the budget
    assert solcast.calls == 3 and service.state.solcast_calls == {"2026-10-18": 3}
    # Not planned with: the sun still comes from Open-Meteo.
    assert service.health()["solcast"]["on"]


def test_no_key_no_solcast(make):
    service, _ = make()
    assert service.solcast is None and service._next_solcast is None and service.cfg.solcast_key is None


def test_review_puts_solcast_next_to_the_panels():
    history = fixture("deye_history_power_20261009_17.json")["stationDataItems"]
    rows = [{"at": "2026-10-15T22:04:00+03:00", "kind": "solcast", "days": {"2026-10-16": [2.4, 1.1, 3.9]},
             "hours": {f"2026-10-16T{h:02d}": [300, 150, 500] for h in range(8, 17)}}]
    lines = solcast_lines(rows, history, date(2026, 10, 16))
    assert lines[0].startswith("panels 16.10: 1.7") and "hours usable" in lines[0]
    assert lines[1].startswith("solcast day-ahead (15.10 22:04): P50 2.4 / P10 1.1 / P90 3.9 kWh; hourly error ")
    assert lines[2] == "solcast morning (15.10 22:04): P50 2.4 / P10 1.1 / P90 3.9 kWh; hourly error " + lines[1].split("hourly error ")[1]


# ---- the sun on the panels' plane ----

def test_the_sun_is_irradiance_on_the_panels_plane():
    multi, ens = fixture("openmeteo_gti_models_20261019.json"), fixture("openmeteo_gti_ensemble_20261019.json")
    top = ["ukmo_seamless", "ecmwf_ifs025", "meteofrance_seamless"]
    fc = combine_forecast(multi, ens, 4.4, T(19, 0), top, 3)
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
    assert "\n\n🔋 <b>Таблиця TOU на завтра, 18.10</b>\n<pre>" in text and text.endswith("⏭ Застосую о 16:00")
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
    # 15:10 in the 15:00-17:00 outage: 30% at ~900 W lasts about an hour, the grid is due at 17:00 (+30 min).
    service, clock = make(now=T(17, 15, 10))
    service.sources.reading = {"soc": 30.0, "grid_present": False, "load_w": 950.0, "pv_w": 50.0}
    await service.watch(clock["now"])
    text = service.messenger.texts[-1]
    assert text.startswith("🪫 <b>Батареї може не вистачити до світла</b>\nЗаряд 30%, навантаження ~900 Вт")
    assert "вистачить приблизно до 16:1" in text and "За графіком світло о 17:00." in text
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
    service.sources.reading = {"soc": 25.0, "grid_present": False, "load_w": 600.0, "pv_w": 0.0}
    await service.watch(clock["now"])
    assert service.messenger.texts[-1].endswith("Світла немає поза графіком.\nВарто зменшити навантаження.")
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
