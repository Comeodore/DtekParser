import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import ConfigError, load_settings
from app.inverter.deye import DeyeError, InverterMonitor, parse_device_latest
from app.service import Service
from app.storage.memory import MemoryStore
from tests.conftest import BASE_ENV, fixture, kyiv

DEYE_ENV = {
    "DEYE_APP_ID": "202511086802012",
    "DEYE_APP_SECRET": "0" * 32,
    "DEYE_EMAIL": "owner@example.com",
    "DEYE_PASSWORD_SHA256": "A" * 64,
    "DEYE_STATION_ID": "61918417",
}


def device(**changes) -> dict:
    """The inverter's reading from 16.10 17:22: grid on, battery charging from it, panels idle."""
    item = fixture("krem_deye_device_latest_20261016_1722.json")["deviceDataList"][0]
    for point in item["dataList"]:
        if point["key"] in changes:
            point["value"] = changes.pop(point["key"])
    item["dataList"] += [{"key": k, "value": v, "unit": ""} for k, v in changes.items()]
    return item


def test_reading_follows_deye_signs():
    r = parse_device_latest(device(), min_grid_voltage=100)
    assert r.at == kyiv(2026, 10, 16, 17, 22, 3)
    assert (r.soc, r.battery_w, r.load_w, r.pv_w, r.grid_w) == (78, 2613, 141, 0, 2861)
    assert r.grid_present and r.grid_v == 220.5


def test_grid_presence_comes_from_voltage():
    assert not parse_device_latest(device(GridVoltageL1L2="0.00"), 100).grid_present
    assert not parse_device_latest(device(GridVoltageL1L2="62.00"), 100).grid_present
    three_phase = device()
    three_phase["dataList"] = [p for p in three_phase["dataList"] if p["key"] != "GridVoltageL1L2"]
    three_phase["dataList"].append({"key": "GridVoltageL1", "value": "231.0", "unit": "V"})
    assert parse_device_latest(three_phase, 100).grid_present


def test_missing_points_are_none_not_zero():
    item = device(SOC="--")
    item["dataList"] = [p for p in item["dataList"] if p["key"] != "TotalDCInputPower"]
    r = parse_device_latest(item, 100)
    assert r.soc is None and r.pv_w is None
    with pytest.raises(DeyeError):
        parse_device_latest({**device(), "collectionTime": None}, 100)


def test_payload_marks_old_readings():
    r = parse_device_latest(device(), 100)
    fresh = r.to_dict(kyiv(2026, 10, 16, 17, 30), stale_after_s=900)
    assert fresh["time"] == "17:22" and not fresh["stale"]
    assert r.to_dict(kyiv(2026, 10, 16, 17, 40), stale_after_s=900)["stale"]
    assert r.to_dict(kyiv(2026, 10, 17, 7, 0), stale_after_s=900)["time"] == "16.10 17:22"


class FakeReader:
    def __init__(self, *results):
        self.results = list(results)

    async def latest(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


async def test_monitor_keeps_last_reading_and_backs_off():
    monitor = InverterMonitor(load_settings({**BASE_ENV, **DEYE_ENV}).deye, clock=lambda: kyiv(2026, 10, 16, 17, 23))
    reading = parse_device_latest(device(), 100)
    assert await monitor.poll(FakeReader(reading)) == 60
    waits = [await monitor.poll(FakeReader(DeyeError("rate limited"))) for _ in range(3)]
    assert waits == [120, 240, 480]
    assert monitor.latest is reading and monitor.failures == 3 and "rate limited" in monitor.last_error
    assert await monitor.poll(FakeReader(reading)) == 60 and monitor.failures == 0


def test_deye_settings():
    deye = load_settings({**BASE_ENV, **DEYE_ENV}).deye
    assert (deye.source, deye.station_id, deye.device_sn, deye.interval) == ("krem", 61918417, None, 60)
    assert deye.password_sha256 == "a" * 64
    assert deye.base_url == "https://eu1-developer.deyecloud.com"
    assert load_settings(BASE_ENV).deye is None


@pytest.mark.parametrize("change, message", [
    ({"DEYE_PASSWORD_SHA256": "plain password"}, "sha256"),
    ({"DEYE_SOURCE": "nope"}, "not in SOURCES"),
    ({"DEYE_STATION_ID": ""}, "DEYE_STATION_ID"),
])
def test_bad_deye_settings(change, message):
    with pytest.raises(ConfigError, match=message):
        load_settings({**BASE_ENV, **DEYE_ENV, **change})


def test_only_the_inverters_source_gets_it(monkeypatch):
    service = Service(load_settings({**BASE_ENV, **DEYE_ENV}), store=MemoryStore(), gateway=object())
    service.inverter.latest = parse_device_latest(device(), 100)
    monkeypatch.setattr("app.api.now_kyiv", lambda: kyiv(2026, 10, 16, 17, 25))
    http = TestClient(create_app(service))
    inverter = http.get("/api/v1/schedule?source=krem").json()["inverter"]
    assert inverter["grid_w"] == 2861 and inverter["battery_w"] == 2613 and inverter["time"] == "17:22"
    assert http.get("/api/v1/schedule?source=kem").json()["inverter"] is None
    assert http.get("/api/v1/health").json()["inverter"]["source"] == "krem"
