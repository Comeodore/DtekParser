"""dtek-service's side of deye-reserve: the inverter endpoint and the webhooks."""
import json
from dataclasses import replace

import pytest

from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi.testclient import TestClient

from app.api import create_app
from app.collector import SourceCollector
from app.config import ConfigError, load_settings
from app.inverter.deye import InverterMonitor, parse_device_latest
from app.notify.router import Router
from app.power.state import PowerState, PowerStatus
from app.reserve_hook import ReserveHook
from app.service import Service
from app.storage.memory import MemoryStore
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
