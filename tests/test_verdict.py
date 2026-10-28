from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import load_settings
from app.dtek.models import Outage, OutageKind
from app.inverter.deye import InverterMonitor, parse_device_latest
from app.power.monitor import PowerMonitor
from app.power.state import PowerState, PowerStatus
from app.power.verdict import Actual, power_status
from app.notify.router import Router
from app.service import Service
from app.storage.memory import MemoryStore
from tests.conftest import BASE_ENV, day, emergency, kyiv, one, slots, snapshot
from tests.test_inverter import BATTERIES_ENV, DEYE_ENV, device

TODAY = date(2026, 10, 28)


def kem_28_10(outage=True):
    """28.10 at kem: off 09:00-12:30 by the schedule, 16:30-20:00 again; DTEK's notice said 09:13-12:30."""
    notice = Outage(OutageKind.OTHER, "Стабілізаційне відключення (Згідно графіку погодинних відключень)",
                    "09:13 28.10.2026", "12:30 28.10.2026") if outage else None
    return snapshot([one(TODAY, [9, 10, 11, 17, 18, 19], first=[12], second=[16])], outage=notice)


def on(since, source="home_assistant"):
    return Actual(True, since, source)


def off(since, source="home_assistant"):
    return Actual(False, since, source)


def test_no_sensor_no_power_status():
    assert power_status(None, kem_28_10(), kyiv(2026, 10, 28, 12, 22), "en") is None


def test_power_back_before_the_scheduled_end():
    # The screenshot of 28.10 12:22: DTEK still had the outage until 12:30, HA saw the power back at 12:15.
    status = power_status(on(kyiv(2026, 10, 28, 12, 15, 29)), kem_28_10(), kyiv(2026, 10, 28, 12, 22), "en")
    assert status == {
        "state": "on", "since": "12:15", "since_at": "2026-10-28T12:15:29+02:00", "source": "home_assistant",
        "expected": {"state": "off", "kind": "schedule", "until": "12:30"},
        "verdict": "early", "due": None,
    }


@pytest.mark.parametrize("actual, at, expected, verdict, due", [
    # Off as scheduled, before DTEK's notice came.
    (off(kyiv(2026, 10, 28, 9, 15)), kyiv(2026, 10, 28, 9, 20), {"state": "off", "kind": "schedule", "until": "12:30"},
     "agree", None),
    # On as scheduled.
    (on(kyiv(2026, 10, 28, 12, 15)), kyiv(2026, 10, 28, 14), {"state": "on", "kind": "schedule", "until": None},
     "agree", None),
    # The 16:30 outage has not begun at 16:40.
    (on(kyiv(2026, 10, 28, 12, 15)), kyiv(2026, 10, 28, 16, 40), {"state": "off", "kind": "schedule", "until": "20:00"},
     "not_yet", None),
    # Still off at 12:45 though the schedule had it back at 12:30.
    (off(kyiv(2026, 10, 28, 9, 15)), kyiv(2026, 10, 28, 12, 45), {"state": "on", "kind": "schedule", "until": None},
     "late", "12:30"),
    # Off at 14:00, between the schedule's outages.
    (off(kyiv(2026, 10, 28, 14)), kyiv(2026, 10, 28, 14, 10), {"state": "on", "kind": "schedule", "until": None},
     "unscheduled", None),
    # The building's inverters keep no history: on during an outage, but since when is unknown.
    (on(None, "deye_batteries"), kyiv(2026, 10, 28, 10), {"state": "off", "kind": "schedule", "until": "12:30"},
     "differs", None),
    (off(None, "deye_batteries"), kyiv(2026, 10, 28, 14), {"state": "on", "kind": "schedule", "until": None},
     "differs", None),
])
def test_power_against_the_schedule(actual, at, expected, verdict, due):
    status = power_status(actual, kem_28_10(outage=False), at, "en")
    assert (status["expected"], status["verdict"], status["due"]) == (expected, verdict, due)


def test_dtek_notices_while_in_force_only():
    snap = snapshot([one(date(2026, 10, 1), [12])], outage=emergency())        # 11:04-15:04, not in the schedule
    during = power_status(off(kyiv(2026, 10, 1, 11, 5)), snap, kyiv(2026, 10, 1, 14), "uk")
    assert during["expected"] == {"state": "off", "kind": "emergency", "until": "15:04"} and during["verdict"] == "agree"
    # Past its restoration time DTEK's notice no longer counts; an outage it covered is running late.
    late = power_status(off(kyiv(2026, 10, 1, 11, 5)), snap, kyiv(2026, 10, 1, 15, 30), "uk")
    assert late["expected"]["state"] == "on" and (late["verdict"], late["due"]) == ("late", "15:04")
    # A notice announced ahead does not count yet either.
    ahead = replace(snap, outage=emergency(start="18:00 01.10.2026", end="22:00 01.10.2026"))
    assert power_status(on(kyiv(2026, 10, 1, 7)), ahead, kyiv(2026, 10, 1, 9), "uk")["verdict"] == "agree"


def test_a_notice_the_schedule_does_not_show_is_dtek_s():
    other = Outage(OutageKind.OTHER, "", "13:00 01.10.2026", "15:00 01.10.2026")
    snap = snapshot([one(date(2026, 10, 1), [20])], outage=other)
    status = power_status(on(kyiv(2026, 10, 1, 13, 40)), snap, kyiv(2026, 10, 1, 14), "en")
    assert status["expected"] == {"state": "off", "kind": "dtek", "until": "15:00"} and status["verdict"] == "early"


def test_some_lines_off_is_no_disagreement():
    snap = snapshot([day(date(2026, 10, 1), ("GPV3.1", slots(off=[12, 13])), ("GPV2.2", slots(off=[14])))], source="krem")
    status = power_status(on(kyiv(2026, 10, 1, 8)), snap, kyiv(2026, 10, 1, 12, 15), "uk")
    assert status["expected"] == {"state": "maybe", "kind": "schedule", "until": "14:00"} and status["verdict"] == "agree"


def test_an_outage_into_tomorrow_ends_tomorrow():
    today = one(date(2026, 10, 1), [22, 23])
    snap = snapshot([today, one(date(2026, 10, 2), [0, 1])], source="krem")
    status = power_status(off(kyiv(2026, 10, 1, 22, 3)), snap, kyiv(2026, 10, 1, 23, 10), "uk")
    assert status["expected"]["until"] == "завтра 02:00" and status["since"] == "22:03"


def test_without_a_schedule_or_notice_only_the_power_is_known():
    suspended = snapshot([one(date(2026, 10, 1), [12])], outage=emergency(start="11:04 30.09.2026",
                                                                           end="09:00 01.10.2026", suspended=True))
    status = power_status(off(kyiv(2026, 9, 30, 11, 6)), suspended, kyiv(2026, 10, 1, 12), "en")
    assert (status["since"], status["expected"], status["verdict"]) == ("11:06 30.09", None, None)
    assert power_status(on(kyiv(2026, 10, 1, 8)), None, kyiv(2026, 10, 1, 12), "en")["expected"] is None


@pytest.fixture
def home(settings):
    now = [kyiv(2026, 10, 28, 12, 20)]
    mon = PowerMonitor(settings.power, MemoryStore(), Router(settings), snapshot=lambda: None, clock=lambda: now[0])
    return mon, now


async def test_home_power_is_known_while_ha_is(home):
    mon, now = home
    v = mon._config.voltage_entity
    assert mon.actual(now[0]) is None                                   # HA has not said anything yet
    await mon._handle({"id": 1, "type": "event", "event": {"a": {
        v: {"s": "231.8", "lc": kyiv(2026, 10, 28, 12, 15, 29).timestamp()}}}})
    mon.connected = True
    assert mon.actual(now[0]) == Actual(True, kyiv(2026, 10, 28, 12, 15, 29), "home_assistant")
    # A dropped websocket is back within seconds: the state holds meanwhile, not for longer.
    mon._disconnected()
    now[0] += timedelta(minutes=1)
    assert mon.actual(now[0]) is not None
    now[0] += timedelta(minutes=2)
    assert mon.actual(now[0]) is None
    mon.connected = True
    await mon._handle({"id": 1, "type": "event", "event": {"c": {v: {"+": {"s": "unavailable"}}}}})
    assert mon.actual(now[0]) is None                                   # the sensor has no value


async def test_never_connected_after_a_start_is_unknown(home):
    mon, now = home
    mon._state = PowerState(PowerStatus.OFF, kyiv(2026, 10, 28, 9, 15))  # restored from the database
    mon._disconnected()                                                 # the first connect failed
    assert mon.actual(now[0]) is None


def test_the_building_s_inverters_when_they_agree():
    monitor = InverterMonitor(load_settings({**BASE_ENV, **DEYE_ENV, **BATTERIES_ENV}).deye)
    now = kyiv(2026, 10, 16, 18, 10)
    reading = parse_device_latest(device(), 100)
    assert monitor.batteries_actual(now) is None
    monitor.batteries["elevator"] = replace(reading, at=kyiv(2026, 10, 16, 18, 9))
    monitor.batteries["water"] = replace(reading, at=kyiv(2026, 10, 16, 18, 8), grid_present=False)
    assert monitor.batteries_actual(now) is None                        # they disagree
    monitor.batteries["water"] = replace(reading, at=kyiv(2026, 10, 16, 17, 40), grid_present=False)
    assert monitor.batteries_actual(now) == Actual(True, None, "deye_batteries")   # the outdated one is left out


def test_each_mini_app_gets_its_own_sensor(monkeypatch):
    service = Service(load_settings({**BASE_ENV, **DEYE_ENV, **BATTERIES_ENV}), store=MemoryStore(), gateway=object())
    now = kyiv(2026, 10, 28, 12, 22)
    service.collectors["kem"].latest = kem_28_10()
    service.collectors["krem"].latest = snapshot([one(TODAY, [12])], source="krem")
    reading = replace(parse_device_latest(device(), 100), at=kyiv(2026, 10, 28, 12, 21))
    service.inverter.latest = reading
    service.inverter._grid = PowerState(PowerStatus.ON, kyiv(2026, 10, 28, 9, 0, 35))
    monkeypatch.setattr("app.api.now_kyiv", lambda: now)
    http = TestClient(create_app(service))
    # HA has not connected: kem falls back to the building's inverters, which have none yet either.
    assert http.get("/api/v1/schedule?source=kem").json()["power"] is None
    service.inverter.batteries["water"] = reading
    kem = http.get("/api/v1/schedule?source=kem").json()["power"]
    assert (kem["state"], kem["source"], kem["verdict"]) == ("on", "deye_batteries", "differs")
    service.power._state = PowerState(PowerStatus.ON, kyiv(2026, 10, 28, 12, 15, 29))
    service.power.connected = True
    kem = http.get("/api/v1/schedule?source=kem").json()["power"]
    assert (kem["state"], kem["since"], kem["source"], kem["verdict"]) == ("on", "12:15", "home_assistant", "early")
    # krem keeps DTEK's badge: its diagram shows the inverter's grid.
    assert http.get("/api/v1/schedule?source=krem").json()["power"] is None
