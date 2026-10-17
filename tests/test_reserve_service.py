"""deye-reserve around the planner: inputs, messages, events, and the (normally off) write path."""
import copy
from datetime import date, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from deye_reserve.config import ConfigError, load_config
from deye_reserve.deye import parse_tou, tou_request
from deye_reserve.inputs import InputError, parse_schedule, schedule_changed
from deye_reserve.planner import Params, Slot, safe_table
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
    days = parse_schedule(SCHEDULE, "GPV2.2", T(17, 15, 30), MAX_AGE).days
    sent = {d.isoformat(): list(s) for d, s in days.items()}
    assert not schedule_changed(sent, days, date(2026, 10, 17))
    later = parse_schedule(with_tomorrow(SCHEDULE, {9, 10}), "GPV2.2", T(17, 15, 30), MAX_AGE).days
    assert schedule_changed(sent, later, date(2026, 10, 17))
    # Midnight: yesterday leaving is not a change.
    assert not schedule_changed({"2026-10-16": ["power_off"] * 24, **sent}, days, date(2026, 10, 17))


# ---- the service, with fakes at the edges ----

class Sources:
    def __init__(self, clock):
        self.payload = copy.deepcopy(SCHEDULE)
        self.reading = {"soc": 82.0, "grid_present": True}      # taken a minute ago unless "at" is given
        self.weather_ok = True
        self._clock = clock

    async def schedule(self):
        return self.payload

    async def inverter(self):
        return {"source": "krem", "reading": {"at": (self._clock() - timedelta(minutes=1)).isoformat(), **self.reading}}

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
    await service.refresh_forecast(now)
    await service.refresh_load(now)


async def test_plan_is_sent_once_and_again_when_the_schedule_changes(make):
    service, clock = make()
    await ready(service, clock["now"])
    report = await service.replan(clock["now"], "test")
    assert report.fallback is None and report.plan.safe
    assert len(service.messenger.texts) == 1
    text = service.messenger.texts[0]
    assert "План батареї на ніч" in text and "сьогодні 17.10: 00:00–01:00, 15:00–17:00" in text
    assert "завтра 18.10: ще не опубліковано" in text and "Лише рекомендація" in text
    assert "Графік на завтра ще не опубліковано" in text
    clock["now"] += timedelta(minutes=30)
    await service.replan(clock["now"], "hourly")
    assert len(service.messenger.texts) == 1                         # nothing changed: quiet
    service.sources.payload = with_tomorrow(service.sources.payload, {9, 10, 11})
    clock["now"] += timedelta(minutes=5)
    await service.replan(clock["now"], "schedule")
    assert len(service.messenger.texts) == 2 and "завтра 18.10: 09:00–12:00" in service.messenger.texts[1]


async def test_no_forecast_means_the_safe_table(make):
    service, clock = make()
    service.sources.weather_ok = False
    await ready(service, clock["now"])
    report = await service.replan(clock["now"], "test")
    assert report.table == safe_table(service.p) and report.fallback == "немає прогнозу сонця"
    assert "безпечна таблиця 90/45" in service.messenger.texts[0]


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
    assert not service.cfg.write and service.deye.updates == []
    assert "У інверторі зараз:" in service.messenger.texts[0]


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


async def test_write_waits_for_the_inverter_and_the_cloud(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true")
    await ready(service, clock["now"])
    service.deye.statuses = [100, 300, 666]
    report = await service.replan(clock["now"], "test")
    assert "Записую в інвертор" in service.messenger.texts[0]
    await next(iter(service._tasks))
    assert len(service.deye.updates) == 1 and service.deye.updates[0]["deviceSn"] == "2511090098"
    assert service.messenger.texts[-1].startswith("✅ <b>Таблицю TOU записано</b>")
    assert service.state.written_table == [[s.start, s.soc, s.grid_charge] for s in report.table]
    assert service.state.writes_on(date(2026, 10, 17)) == 1
    await service.replan(clock["now"] + timedelta(hours=1), "hourly")
    assert len(service.deye.updates) == 1                            # already there: no second write


async def test_failed_write_is_an_alarm(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true")
    await ready(service, clock["now"])
    service.deye.statuses = [300, 500]
    original = service.deye.order

    async def failing(order_id):
        result = await original(order_id)
        return {**result, "error": "530"} if result["status"] == 500 else result
    service.deye.order = failing
    await service.replan(clock["now"], "test")
    await next(iter(service._tasks))
    assert service.messenger.texts[-1].startswith("🚨 <b>Запис TOU не вдався</b>")
    assert "інвертор не виконав команду" in service.messenger.texts[-1]
    assert service.write_blocker(service.report, clock["now"]).startswith("після помилки чекаю")


async def test_write_guards(make):
    service, clock = make(now=T(17, 18), DEYE_RESERVE_WRITE="true")
    await ready(service, clock["now"])
    service.sources.reading = {**service.sources.reading, "grid_present": False}
    await service.replan(clock["now"], "test")
    assert service.deye.updates == [] and "Поки не записую: немає мережі" in service.messenger.texts[0]
    report = service.report
    report.grid_present = True
    assert service.write_blocker(report, T(17, 16)) == "триває відключення за графіком"
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
    cfg = load_config(ENV)
    assert (cfg.write, cfg.port, cfg.host, cfg.group, cfg.chat_id) == (False, 9997, "127.0.0.1", "GPV2.2", "370921204")
    assert cfg.params == Params() and cfg.debounce_s == 150 and cfg.max_writes_per_day == 6
    assert [t.isoformat() for t in cfg.forecast_at] == ["05:30:00", "22:00:00"]
    assert load_config({**ENV, "DEYE_RESERVE_CHARGE_KW": "3.1", "DEYE_RESERVE_NIGHT_MAX": "90"}).params.charge_kw == 3.1
    with pytest.raises(ConfigError, match="DEYE_RESERVE_CHAT_ID"):
        load_config({k: v for k, v in ENV.items() if k != "DEYE_RESERVE_CHAT_ID"})
    with pytest.raises(ConfigError, match="SHUTDOWN_SOC"):
        load_config({**ENV, "DEYE_RESERVE_DAY_FLOOR": "15"})
    with pytest.raises(ConfigError, match="boolean"):
        load_config({**ENV, "DEYE_RESERVE_WRITE": "maybe"})
