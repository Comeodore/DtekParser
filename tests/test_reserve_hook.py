"""dtek-service's side of deye-reserve: the inverter endpoint and the webhooks."""
from dataclasses import replace

from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi.testclient import TestClient

from app.api import create_app
from app.collector import SourceCollector
from app.config import load_settings
from app.inverter.deye import parse_device_latest
from app.notify.router import Router
from app.power.state import PowerState, PowerStatus
from app.reserve_hook import ReserveHook
from app.service import Service
from app.storage.memory import MemoryStore
from tests.conftest import BASE_ENV, kyiv
from tests.test_collector import DAY_OCT1, FakeSite, fact, hours
from tests.test_inverter import DEYE_ENV, FakeReader, device, grid_monitor


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


def test_the_inverter_reports_its_day_of_sun():
    assert parse_device_latest(device(), 100).pv_today_kwh == 1.7
    assert parse_device_latest(device(), 100).to_dict(kyiv(2026, 10, 16, 17, 25), 900)["pv_today_kwh"] == 1.7


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
