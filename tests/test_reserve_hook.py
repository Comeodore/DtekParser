"""dtek-service's side of deye-reserve: the inverter endpoint, the webhooks and the owner's table."""
import hashlib
import hmac
import json
from dataclasses import replace
from urllib.parse import urlencode

import pytest

from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi.testclient import TestClient

from app.api import battery_until, create_app
from app.collector import SourceCollector
from app.config import ConfigError, load_settings
from app.inverter.deye import InverterMonitor, parse_device_latest
from app.notify.router import Router
from app.power.state import PowerState, PowerStatus
from app.reserve_hook import ReserveHook, charge_table, ranges
from app.service import Service
from app.storage.memory import MemoryStore
from app.webapp_auth import webapp_user
from tests.conftest import BASE_ENV, kyiv
from tests.test_collector import DAY_OCT1, FakeSite, fact, hours
from deye_reserve.config import load_config
from tests.test_inverter import DEYE_ENV, FakeReader, device, grid_monitor
from tests.test_reserve_service import ENV as RESERVE_ENV


def test_reserve_url_needs_the_inverter():
    assert load_settings({**BASE_ENV, **DEYE_ENV, "DEYE_RESERVE_URL": "http://127.0.0.1:9997/"}).reserve_url \
        == "http://127.0.0.1:9997/"
    assert load_settings({**BASE_ENV, "DEYE_RESERVE_URL": "http://127.0.0.1:9997"}).reserve_url is None
    assert ReserveHook("http://127.0.0.1:9997/").url == "http://127.0.0.1:9997"


def test_inverter_latest(monkeypatch):
    service = Service(load_settings({**BASE_ENV, **DEYE_ENV}), store=MemoryStore(), gateway=object())
    monkeypatch.setattr("app.api.now_kyiv", lambda: kyiv(2026, 10, 16, 17, 25))
    http = TestClient(create_app(service))
    assert http.get("/api/v1/inverter/latest").json()["reading"] is None
    service.inverter.latest = parse_device_latest(device(), 100)
    service.inverter._grid = PowerState(PowerStatus.ON, kyiv(2026, 10, 16, 9))
    body = http.get("/api/v1/inverter/latest").json()
    assert body["source"] == "krem" and body["grid"] == {"status": "ON", "since": "2026-10-16T09:00:00+03:00"}
    assert (body["reading"]["soc"], body["reading"]["grid_present"], body["reading"]["stale"]) == (78, True, False)
    without = Service(load_settings(BASE_ENV), store=MemoryStore(), gateway=object())
    assert TestClient(create_app(without)).get("/api/v1/inverter/latest").status_code == 404


async def test_collector_tells_when_the_schedule_data_changes(settings):
    calls = []
    site, clock = FakeSite(), {"now": kyiv(2026, 10, 1, 18)}
    collector = SourceCollector(settings.source("kem"), site, MemoryStore(), Router(settings),
                                clock=lambda: clock["now"], on_schedule=calls.append)
    await collector.cycle()
    assert calls == []                                              # the first fetch is no news
    await collector.cycle()
    assert calls == []
    site.fact = fact("01.10.2026 19:10", {DAY_OCT1: {"GPV4.1": hours(21, 22)}})
    await collector.cycle()
    assert calls == ["01.10.2026 19:10"]


async def test_grid_changes_reach_the_hook():
    store, now, seen = MemoryStore(), [kyiv(2026, 10, 16, 18, 0)], []
    monitor = grid_monitor(store, now)
    monitor._on_grid = lambda t: seen.append(t.status)
    await monitor.restore()
    on = replace(parse_device_latest(device(), 100), at=kyiv(2026, 10, 16, 17, 59))
    await monitor.poll(FakeReader(on))
    assert seen == []                                               # first sight only initialises
    now[0] = kyiv(2026, 10, 16, 18, 2, 5)
    await monitor.poll(FakeReader(replace(on, at=kyiv(2026, 10, 16, 18, 1, 12), grid_present=False)))
    assert seen == [PowerStatus.OFF]


async def test_hook_posts_and_never_raises():
    got = []

    async def receive(request):
        got.append((request.match_info["kind"], await request.json()))
        return web.json_response({}, status=202)
    app = web.Application()
    app.router.add_post("/events/{kind}", receive)
    async with TestServer(app) as server:
        hook = ReserveHook(str(server.make_url("/")))
        hook.fire("schedule", "17.10.2026 14:15")
        await next(iter(hook._tasks))
    assert got == [("schedule", {"detail": "17.10.2026 14:15"})] and hook.sent == 1
    dead = ReserveHook("http://127.0.0.1:9")
    dead.fire("grid", "lost")
    await next(iter(dead._tasks))
    assert dead.failures == 1
    ReserveHook(None).fire("grid", "lost")                          # not configured: nothing at all


def test_the_inverter_reports_its_day_of_sun_and_grid():
    reading = parse_device_latest(device(), 100)
    assert (reading.pv_today_kwh, reading.grid_today_kwh) == (1.7, 8.0)
    day = reading.to_dict(kyiv(2026, 10, 16, 17, 25), 900)
    assert (day["pv_today_kwh"], day["grid_today_kwh"]) == (1.7, 8.0)


def test_the_krem_mini_app_gets_todays_sun_forecast(monkeypatch):
    service = Service(load_settings({**BASE_ENV, **DEYE_ENV, "DEYE_RESERVE_URL": "http://127.0.0.1:9997"}),
                      store=MemoryStore(), gateway=object())
    service.inverter.latest = parse_device_latest(device(), 100)
    monkeypatch.setattr("app.api.now_kyiv", lambda: kyiv(2026, 10, 16, 17, 25))

    async def sun():
        return {"date": "2026-10-16", "p50_kwh": 2.4, "low_kwh": 1.2}
    service.reserve.sun = sun
    http = TestClient(create_app(service))
    inv = http.get("/api/v1/schedule?source=krem").json()["inverter"]
    assert (inv["pv_today_kwh"], inv["pv_forecast_kwh"]) == (1.7, 2.4)

    async def yesterday():
        return {"date": "2026-10-15", "p50_kwh": 9.9}
    service.reserve.sun = yesterday
    assert "pv_forecast_kwh" not in http.get("/api/v1/schedule?source=krem").json()["inverter"]


def test_the_tariff_hours_are_deye_reserves():
    for env in ({}, {"DEYE_RESERVE_NIGHT_START": "22", "DEYE_RESERVE_NIGHT_END": "6"}):
        deye, reserve = load_settings({**BASE_ENV, **DEYE_ENV, **env}).deye, load_config({**RESERVE_ENV, **env}).params
        assert (deye.night_start_h, deye.night_end_h) == (reserve.night_start_h, reserve.night_end_h)
    with pytest.raises(ConfigError, match="NIGHT_END"):
        load_settings({**BASE_ENV, **DEYE_ENV, "DEYE_RESERVE_NIGHT_END": "23"})


def grid_at(h: int, m: int, s: int, kwh: float, d: int = 16):
    return replace(parse_device_latest(device(), 100), at=kyiv(2026, 10, d, h, m, s), grid_today_kwh=kwh)


def grid_split(monitor):
    body = monitor.payload(monitor.latest.at)
    return body["grid_night_kwh"], body["grid_day_kwh"]


async def test_the_grid_day_is_split_by_tariff_and_kept_across_a_restart(tmp_path):
    path = str(tmp_path / "grid-day-krem.json")
    monitor = InverterMonitor(load_settings({**BASE_ENV, **DEYE_ENV}).deye, grid_day_path=path)
    seen = []
    for reading in (grid_at(6, 58, 50, 2.1), grid_at(7, 0, 2, 2.2), grid_at(18, 0, 0, 5.0),
                    grid_at(22, 59, 30, 7.6), grid_at(23, 0, 42, 7.7), grid_at(23, 58, 0, 9.6)):
        await monitor.poll(FakeReader(reading))
        seen.append(grid_split(monitor))
    # Night until 07:00; then the counter at 07:00 is the night, and after 23:00 what it gained since.
    assert seen == [(2.1, 0.0), (2.1, 0.1), (2.1, 2.9), (2.1, 5.5), (2.2, 5.5), (4.1, 5.5)]
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"day": "2026-10-16", "marks": {"7": 2.1, "23": 7.6}}
    restarted = InverterMonitor(load_settings({**BASE_ENV, **DEYE_ENV}).deye, grid_day_path=path)
    await restarted.restore()
    await restarted.poll(FakeReader(grid_at(23, 59, 10, 9.7)))
    assert grid_split(restarted) == (4.2, 5.5)
    # The next day starts over: all night until 07:00, its own counter at 07:00 after that.
    await restarted.poll(FakeReader(grid_at(0, 0, 30, 0.1, d=17)))
    assert grid_split(restarted) == (0.1, 0.0)
    await restarted.poll(FakeReader(grid_at(6, 59, 0, 3.0, d=17)))
    await restarted.poll(FakeReader(grid_at(7, 0, 10, 3.0, d=17)))
    assert grid_split(restarted) == (3.0, 0.0)


async def test_a_missed_tariff_hour_leaves_the_grid_day_whole(tmp_path):
    deye = load_settings({**BASE_ENV, **DEYE_ENV}).deye
    late = InverterMonitor(deye)                                    # started after 07:00
    await late.poll(FakeReader(grid_at(9, 0, 0, 3.0)))
    assert grid_split(late) == (None, None) and late.payload(late.latest.at)["grid_today_kwh"] == 3.0
    gap = InverterMonitor(deye)                                     # no reading from 06:40 to 07:15
    await gap.poll(FakeReader(grid_at(6, 40, 0, 2.0)))
    await gap.poll(FakeReader(grid_at(7, 15, 0, 2.6)))
    assert grid_split(gap) == (None, None)
    early = InverterMonitor(deye)                                   # the inverter's midnight came before 23:59
    for reading in (grid_at(6, 59, 0, 2.0), grid_at(7, 0, 5, 2.0), grid_at(22, 59, 0, 7.0),
                    grid_at(23, 0, 10, 7.1), grid_at(23, 59, 0, 0.1)):
        await early.poll(FakeReader(reading))
    assert grid_split(early) == (None, None)
    broken = tmp_path / "grid-day-krem.json"
    broken.write_text("{not json", encoding="utf-8")
    InverterMonitor(deye, grid_day_path=str(broken)).grid_day.restore()   # logged, not raised


def test_the_krem_mini_app_gets_the_grid_by_tariff(monkeypatch, tmp_path):
    service = Service(load_settings({**BASE_ENV, **DEYE_ENV, "STATE_DIR": str(tmp_path)}),
                      store=MemoryStore(), gateway=object())
    service.inverter.grid_day.follow(grid_at(6, 59, 0, 2.5), grid_at(7, 0, 12, 2.6))
    service.inverter.latest = parse_device_latest(device(), 100)
    monkeypatch.setattr("app.api.now_kyiv", lambda: kyiv(2026, 10, 16, 17, 25))
    inv = TestClient(create_app(service)).get("/api/v1/schedule?source=krem").json()["inverter"]
    assert (inv["grid_today_kwh"], inv["grid_night_kwh"], inv["grid_day_kwh"]) == (8.0, 2.5, 5.5)
    assert (tmp_path / "grid-day-krem.json").exists()


async def test_the_reserve_gets_the_krem_inverters_day(tmp_path):
    service = Service(load_settings({**BASE_ENV, **DEYE_ENV, "STATE_DIR": str(tmp_path),
                                     "DEYE_RESERVE_URL": "http://127.0.0.1:9997"}),
                      store=MemoryStore(), gateway=object())
    service.inverter.grid_day.follow(grid_at(6, 59, 0, 2.5), grid_at(7, 0, 12, 2.6))
    sun = {"date": "2026-10-16", "p50_kwh": 6.4, "days": {"2026-10-16": 6.4, "2026-10-17": 3.2}}

    async def reserve_sun():
        return sun
    service.reserve.sun = reserve_sun
    grid_day = {"day": "2026-10-16", "marks": {"7": 2.5}, "night_start_h": 23, "night_end_h": 7}
    assert await service.inverter_day("krem") == {"grid_day": grid_day, "sun": sun["days"]}
    assert await service.inverter_day("kem") is None
    # deye-reserve away (or of before its `days`): the counter alone.
    sun = None
    assert await service.inverter_day("krem") == {"grid_day": grid_day}


def test_the_grid_day_takes_marks_noted_elsewhere():
    grid = InverterMonitor(load_settings({**BASE_ENV, **DEYE_ENV}).deye).grid_day
    assert grid.payload() == {"day": None, "marks": {}, "night_start_h": 23, "night_end_h": 7}
    grid.add("2026-10-16", {"23": 7.6})
    grid.add("2026-10-16", {"7": 2.1, "23": 7.5})              # the same day's: taken over these
    grid.add("2026-10-15", {"7": 9.9})                         # an earlier day's: left out
    grid.add("2026-10-16", {"7": "not a number"})              # broken: left out whole
    grid.add(None, {"7": 1.0})
    assert grid.payload()["marks"] == {"7": 2.1, "23": 7.5}
    assert grid.split(grid_at(23, 40, 0, 8.0)) == (2.6, 5.4)
    grid.add("2026-10-17", {"7": 0.3})                         # a later day's: replace them
    assert grid.payload()["day"] == "2026-10-17" and grid.payload()["marks"] == {"7": 0.3}


# ---- deye-reserve's table in the krem mini app, for the owner only ----

def slots(*rows):
    return [{"start": a, "soc": b, "grid_charge": c} for a, b, c in rows]


NIGHT_70 = slots((0, 70, True), (420, 45, False), (660, 45, False), (900, 45, False), (1140, 45, False),
                 (1380, 70, True))
TOP_UP = slots((0, 70, True), (420, 45, False), (660, 45, False), (1020, 61, True), (1080, 45, False),
               (1380, 70, True))


def span(rows):
    return [(r["start"], r["end"], r["soc"], r["charge"]) for r in rows]


def test_slots_in_a_row_with_the_same_target_are_one_range():
    as_lists = lambda t: [[s["start"], s["soc"], s["grid_charge"]] for s in t]
    # The night runs on past midnight: 23:00 and 00:00 at 70% with the grid are one range, 23-07.
    assert span(ranges(as_lists(NIGHT_70))) == [(420, 1380, 45, False), (1380, 420, 70, True)]
    assert span(ranges(as_lists(TOP_UP))) == [(420, 1020, 45, False), (1020, 1080, 61, True), (1080, 1380, 45, False),
                                              (1380, 420, 70, True)]
    # Last night's 75% and tonight's 70% stay apart; so do the same SOC with and without the grid.
    assert span(ranges([[1380, 70, True], [0, 75, True], [420, 45, False], [660, 45, True], [900, 45, False],
                        [1140, 45, False]])) == [(0, 420, 75, True), (420, 660, 45, False), (660, 900, 45, True),
                                                 (900, 1380, 45, False), (1380, 1440, 70, True)]
    assert span(ranges([[m, 45, False] for m in (0, 240, 480, 720, 960, 1200)])) == [(0, 1440, 45, False)]
    assert ranges([]) == []


def plan_body(today, tomorrow, at="2026-10-25T15:00:00+02:00", switch_at="2026-10-25T00:00:00+03:00", **more):
    return {"at": at, "table": today, "tables": {"2026-10-25": today, "2026-10-26": tomorrow},
            "switch_at": switch_at, "fallback": None, "kept_from": None, "write": True,
            "written_table": today, "last_write": {"at": "2026-10-25T07:24:04+02:00", "ok": True}, "why": {}, **more}


def test_today_and_tomorrow_are_one_table_when_they_are_the_same():
    body = charge_table(plan_body(NIGHT_70, NIGHT_70, why={
        "2026-10-25": {"at": "2026-10-25T07:16:00+02:00", "reasons": ["нічний заряд позаду"]},
        "2026-10-26": {"at": "2026-10-25T20:10:00+02:00", "reasons": ["опубліковано графік на 26.10"]},
        "2026-10-24": {"at": "2026-10-24T23:00:00+02:00", "reasons": ["вчорашнє"]}}, at="2026-10-25T20:10:51+02:00"),
        kyiv(2026, 10, 25, 21, 0))
    assert [(b["kind"], b["dates"], b["current"], b["from"]) for b in body["blocks"]] == [("both", "25–26.10", True, None)]
    assert [r["now"] for r in body["blocks"][0]["ranges"]] == [True, False]
    assert body["status"] == "written" and not body["stale"] and body["at"] == "20:10"
    assert body["why"] == {"at": "20:10", "reasons": ["опубліковано графік на 26.10"]}      # the latest shown day's


def test_tomorrows_table_is_shown_when_it_differs_with_the_time_it_takes_over():
    plan = plan_body(NIGHT_70, TOP_UP, switch_at="2026-10-25T23:00:00+02:00")
    body = charge_table(plan, kyiv(2026, 10, 25, 2, 30))                     # in last night's charge
    assert [(b["kind"], b["dates"], b["current"], b["from"]) for b in body["blocks"]] == [
        ("today", "25.10", True, None), ("tomorrow", "26.10", False, "23:00")]
    assert [r.get("now") for r in body["blocks"][0]["ranges"]] == [False, True]
    assert all("now" not in r for r in body["blocks"][1]["ranges"]) and body["status"] == "written"
    # 23:30: tomorrow's took over, and it is the inverter's when written.
    late = charge_table({**plan, "written_table": TOP_UP}, kyiv(2026, 10, 25, 23, 30))
    assert [(b["kind"], b["current"]) for b in late["blocks"]] == [("tomorrow", True)] and late["status"] == "written"
    assert [r["now"] for r in late["blocks"][0]["ranges"]] == [False, False, False, True]
    # Not taken yet, written off, or the last write failed.
    assert charge_table(plan, kyiv(2026, 10, 25, 23, 30))["status"] == "pending"
    assert charge_table({**plan, "write": False, "written_table": None}, kyiv(2026, 10, 25, 23, 30))["status"] == "off"
    failed = {**plan, "last_write": {"at": "2026-10-25T23:00:30+02:00", "ok": False}}
    assert charge_table(failed, kyiv(2026, 10, 25, 23, 30))["status"] == "failed"
    # An old plan says so.
    assert charge_table(plan, kyiv(2026, 10, 25, 17, 30))["stale"]


def init_data(user_id=370921204, token="2:b", signed=None, **fields):
    """initData as Telegram signs it (core.telegram.org/bots/webapps), written out here on its own."""
    fields = {"query_id": "AAHk0BIWAAAAAOTQEhb_x1Yd", "user": json.dumps({"id": user_id, "first_name": "В"}),
              "auth_date": str(int((signed or kyiv(2026, 10, 25, 20, 55)).timestamp())), "signature": "c2lnbmF0dXJl",
              **fields}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def owner_app(monkeypatch, plan=None, **env):
    service = Service(load_settings({**BASE_ENV, **DEYE_ENV, "DEYE_RESERVE_URL": "http://127.0.0.1:9997",
                                     "DEYE_RESERVE_CHAT_ID": "370921204", **env}),
                      store=MemoryStore(), gateway=object())
    monkeypatch.setattr("app.api.now_kyiv", lambda: kyiv(2026, 10, 25, 21, 0))

    async def fake_plan():
        return plan
    service.reserve.plan = fake_plan
    return service, TestClient(create_app(service))


def test_only_the_owner_gets_the_table(monkeypatch):
    service, http = owner_app(monkeypatch, plan_body(NIGHT_70, NIGHT_70))
    assert service.settings.owner_id == 370921204

    def get(data=None, source="krem"):
        headers = {"X-Telegram-Init-Data": data} if data is not None else {}
        return http.get(f"/api/v1/reserve/table?source={source}", headers=headers)
    r = get(init_data())
    assert r.status_code == 200 and r.json()["blocks"][0]["kind"] == "both"
    assert get().status_code == 403                                              # a plain browser
    assert get("").status_code == 403
    assert get(init_data(user_id=12345)).status_code == 403                      # someone else in the krem chats
    assert get(init_data(token="1:a")).status_code == 403                        # signed by another bot
    assert get(init_data(signed=kyiv(2026, 10, 23, 20, 0))).status_code == 403   # two days old
    forged = init_data().replace("370921204", "370921205")
    assert get(forged).status_code == 403
    assert get(init_data() + "&user=%7B%22id%22%3A370921204%7D").status_code == 403    # a field twice
    assert get(init_data(), source="kem").status_code == 404                     # no inverter there
    # deye-reserve not there: no table, and no secret either.
    _, down = owner_app(monkeypatch, None)
    assert down.get("/api/v1/reserve/table?source=krem",
                    headers={"X-Telegram-Init-Data": init_data()}).status_code == 503


def test_the_owner_is_deye_reserves_private_chat_unless_set():
    env = {**BASE_ENV, **DEYE_ENV, "DEYE_RESERVE_URL": "http://127.0.0.1:9997"}
    assert load_settings({**env, "DEYE_RESERVE_CHAT_ID": "370921204"}).owner_id == 370921204
    assert load_settings({**env, "DEYE_RESERVE_CHAT_ID": "-1001234", "DEYE_RESERVE_OWNER_ID": "42"}).owner_id == 42
    assert load_settings({**env, "DEYE_RESERVE_CHAT_ID": "-1001234"}).owner_id is None      # a group
    assert load_settings({**BASE_ENV, **DEYE_ENV, "DEYE_RESERVE_CHAT_ID": "370921204"}).owner_id is None
    with pytest.raises(ConfigError, match="OWNER_ID"):
        load_settings({**env, "DEYE_RESERVE_OWNER_ID": "-5"})


def test_init_data_is_checked_by_telegrams_rule():
    now = kyiv(2026, 10, 25, 21, 0)
    assert webapp_user(init_data(), "2:b", now)["id"] == 370921204
    assert webapp_user(init_data(), "2:b", kyiv(2026, 10, 27, 21, 0)) is None          # over a day old
    assert webapp_user(init_data(user="not json"), "2:b", now) is None
    assert webapp_user("hash=abc", "2:b", now) is None and webapp_user("%%%", "2:b", now) is None
    assert webapp_user(init_data(), "", now) is None



def test_the_krem_mini_app_shows_the_time_left_after_a_battery_warning(monkeypatch):
    service = Service(load_settings({**BASE_ENV, **DEYE_ENV, "DEYE_RESERVE_URL": "http://127.0.0.1:9997"}),
                      store=MemoryStore(), gateway=object())
    service.inverter.latest = replace(parse_device_latest(device(), 100), grid_present=False)
    monkeypatch.setattr("app.api.now_kyiv", lambda: kyiv(2026, 10, 16, 17, 25))
    warning = {"at": "2026-10-16T17:23:00+03:00", "soc": 20.0, "load_w": 900,
               "empty_at": "2026-10-16T18:28:00+03:00", "back_at": None}

    async def battery():
        return warning
    service.reserve.battery = battery
    http = TestClient(create_app(service))
    assert http.get("/api/v1/schedule?source=krem").json()["inverter"]["battery_until"] == "18:28"
    assert battery_until({**warning, "at": "2026-10-16T17:10:00+03:00"}, {"grid_present": False},
                         kyiv(2026, 10, 16, 17, 25)) is None                    # not the present load any more
    assert battery_until(warning, {"grid_present": True}, kyiv(2026, 10, 16, 17, 25)) is None   # the grid is back
    assert battery_until(None, {"grid_present": False}, kyiv(2026, 10, 16, 17, 25)) is None
