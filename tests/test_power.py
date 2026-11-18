import asyncio

import pytest

from app.config import load_settings
from app.notify.router import Router
from app.power.monitor import PowerMonitor
from app.power.state import PowerState, PowerStatus, next_state, parse_voltage
from app.storage.memory import MemoryStore
from tests.conftest import BASE_ENV, kyiv, one, snapshot


def test_voltage_parsing():
    assert parse_voltage("232.8", 50) is PowerStatus.ON
    assert parse_voltage("0.0", 50) is PowerStatus.OFF
    assert parse_voltage("unavailable", 50) is None
    assert parse_voltage(None, 50) is None


def test_first_observation_only_initialises():
    state, transition = next_state(None, PowerStatus.ON, kyiv(2026, 10, 1, 9), kyiv(2026, 10, 1, 10), "100%", True)
    assert transition is None and state.status is PowerStatus.ON


def test_outage_during_a_disconnect_keeps_its_real_time():
    stored = PowerState(PowerStatus.ON, kyiv(2026, 9, 30, 2, 50))
    went_off = kyiv(2026, 10, 1, 14, 2)
    state, transition = next_state(stored, PowerStatus.OFF, went_off, kyiv(2026, 10, 1, 14, 5), "99%", True)
    assert transition.at == went_off and transition.detected_late
    assert state == PowerState(PowerStatus.OFF, went_off)


def test_live_transition_is_not_late():
    stored = PowerState(PowerStatus.OFF, kyiv(2026, 10, 1, 9))
    _, transition = next_state(stored, PowerStatus.ON, kyiv(2026, 10, 1, 10), kyiv(2026, 10, 1, 10, 0, 1), "80%", False)
    assert not transition.detected_late
    assert transition.duration_seconds == 3600


def test_clock_skew_never_goes_back_in_time():
    stored = PowerState(PowerStatus.ON, kyiv(2026, 10, 1, 10))
    _, transition = next_state(stored, PowerStatus.OFF, kyiv(2026, 10, 1, 9), kyiv(2026, 10, 1, 10, 5), "N/A", False)
    assert transition.at == stored.since


@pytest.fixture
def monitor(settings):
    store = MemoryStore()
    snap = snapshot([one(kyiv(2026, 10, 1).date(), [16, 17, 18])])
    clock = {"now": kyiv(2026, 10, 1, 16, 1)}
    mon = PowerMonitor(settings.power, store, Router(settings), snapshot=lambda: snap, clock=lambda: clock["now"])
    return mon, store, clock


async def test_subscription_snapshot_then_live_changes(monitor):
    mon, store, clock = monitor
    store.data.power = PowerState(PowerStatus.ON, kyiv(2026, 9, 30, 2, 50))
    await mon.restore()
    v, b, src = mon._config.voltage_entity, mon._config.battery_entity, mon._config.input_source_entity

    # Initial snapshot on subscribe: power is on, nothing to report.
    await mon._handle({"id": 1, "type": "event", "event": {"a": {
        v: {"s": "231.8", "lc": kyiv(2026, 10, 1, 15).timestamp()},
        b: {"s": "100.0", "lc": kyiv(2026, 10, 1, 8).timestamp()},
        src: {"s": "GRID", "lc": kyiv(2026, 10, 1, 15).timestamp()},
    }}})
    assert store.queued() == []

    # Voltage noise does not touch the database or produce messages.
    await mon._handle({"id": 1, "type": "event", "event": {"c": {v: {"+": {"s": "232.8", "lc": 1.0}}}}})
    assert store.queued() == []

    went_off = kyiv(2026, 10, 1, 16, 0, 30)
    await mon._handle({"id": 1, "type": "event", "event": {"c": {v: {"+": {"s": "0.0", "lc": went_off.timestamp()}}}}})
    await mon._handle({"id": 1, "type": "event", "event": {"c": {src: {"+": {"s": "NOT_CONNECTED"}}}}})
    queued = store.queued()
    assert [m.chat_id for m in queued] == ["111", "222"]
    assert "POWER OUTAGE" in queued[0].text and "⚡ Scheduled" in queued[0].text
    assert store.data.power == PowerState(PowerStatus.OFF, went_off)


async def test_unavailable_sensor_is_ignored(monitor):
    mon, store, _ = monitor
    store.data.power = PowerState(PowerStatus.ON, kyiv(2026, 9, 30))
    await mon.restore()
    v = mon._config.voltage_entity
    await mon._handle({"id": 1, "type": "event", "event": {"c": {v: {"+": {"s": "unavailable"}}}}})
    assert store.queued() == [] and store.data.power.status is PowerStatus.ON


async def test_home_battery_follows_ha(monitor):
    mon, store, _ = monitor
    await mon.restore()
    soc, power = mon._config.battery_entity, mon._config.battery_power_entity
    assert mon.battery_payload() is None
    await mon._handle({"id": 1, "type": "event", "event": {"a": {
        soc: {"s": "86.0", "lc": 1.0}, power: {"s": "1026.0", "lc": 1.0},
    }}})
    mon.connected = True
    assert mon.battery_payload() == {"name": "home", "soc": 86.0, "battery_w": 1026.0, "stale": False}
    await mon._handle({"id": 1, "type": "event", "event": {"c": {power: {"+": {"s": "-2767.0"}}}}})
    mon.connected = False
    assert mon.battery_payload()["battery_w"] == -2767.0 and mon.battery_payload()["stale"]
    assert store.queued() == []


async def test_empty_database_is_initialised_silently(monitor):
    mon, store, _ = monitor
    await mon.restore()
    v, src = mon._config.voltage_entity, mon._config.input_source_entity
    await mon._handle({"id": 1, "type": "event", "event": {"a": {
        v: {"s": "0.0", "lc": kyiv(2026, 10, 1, 9).timestamp()},
        src: {"s": "NOT_CONNECTED", "lc": kyiv(2026, 10, 1, 9).timestamp()},
    }}})
    assert store.queued() == []
    assert store.data.power.status is PowerStatus.OFF


# ---- 0 V judged by the AC input (sequences as Home Assistant recorded them at home) ----

async def send(mon, at=None, **values):
    names = {"v": mon._config.voltage_entity, "src": mon._config.input_source_entity}
    changed = {}
    for key, value in values.items():
        plus = {"s": value}
        if at is not None:
            plus["lc"] = at.timestamp()
        changed[names[key]] = {"+": plus}
    await mon._handle({"id": 1, "type": "event", "event": {"c": changed}})


@pytest.fixture
async def powered(monitor):
    mon, store, clock = monitor
    clock["now"] = kyiv(2026, 11, 18, 3)
    store.data.power = PowerState(PowerStatus.ON, kyiv(2026, 11, 15, 23, 31))
    await mon.restore()
    mon.connected = True
    await mon._handle({"id": 1, "type": "event", "event": {"a": {
        mon._config.voltage_entity: {"s": "232.8", "lc": kyiv(2026, 11, 15, 23, 31).timestamp()},
        mon._config.input_source_entity: {"s": "GRID", "lc": kyiv(2026, 11, 15, 23, 31).timestamp()},
    }}})
    yield mon, store, clock
    mon._disconnected()


async def test_outage_waits_for_the_input_to_say_not_connected(powered):
    # 17.11.2026: the voltage read 0 at 08:05:31, the input NOT_CONNECTED at 08:05:33.
    mon, store, clock = powered
    went_off = kyiv(2026, 11, 17, 8, 5, 31)
    await send(mon, went_off, v="0.0")
    assert store.queued() == [] and store.data.power.status is PowerStatus.ON
    await send(mon, kyiv(2026, 11, 17, 8, 5, 33), src="NOT_CONNECTED")
    assert len(store.queued()) == 2
    assert store.data.power == PowerState(PowerStatus.OFF, went_off)


async def test_outage_with_the_input_in_the_same_message(powered):
    # 16.11.2026 00:26:26: both changed in the same second.
    mon, store, _ = powered
    went_off = kyiv(2026, 11, 16, 0, 26, 26)
    await send(mon, went_off, v="0.0", src="NOT_CONNECTED")
    assert store.data.power == PowerState(PowerStatus.OFF, went_off) and len(store.queued()) == 2


async def test_a_gx_without_inverter_data_is_no_outage(powered):
    # 18.11.2026 00:50: a Remote VEConfigure download. The voltage read 0 a second before the input read UNKNOWN,
    # then went unavailable, read 0 once more and came back; the input read GRID again last.
    mon, store, clock = powered
    await send(mon, kyiv(2026, 11, 18, 0, 50, 37), v="0.0")
    await send(mon, kyiv(2026, 11, 18, 0, 50, 37), src="UNKNOWN")
    assert mon.energy_reading(clock["now"]) is None
    await send(mon, v="unavailable")
    await send(mon, kyiv(2026, 11, 18, 0, 51, 41), v="0.0")
    await send(mon, kyiv(2026, 11, 18, 0, 51, 42), v="232.8")
    await send(mon, kyiv(2026, 11, 18, 0, 51, 43), src="GRID")
    assert store.queued() == [] and store.data.power.status is PowerStatus.ON
    assert mon.energy_reading(clock["now"]).power is True


async def test_a_gx_restart_is_no_outage(powered):
    # 18.11.2026 02:14: everything unavailable, the input UNKNOWN, the voltage 0 afterwards, both back together.
    mon, store, _ = powered
    await send(mon, v="unavailable", src="unavailable")
    await send(mon, src="UNKNOWN")
    await send(mon, kyiv(2026, 11, 18, 2, 14, 50), v="0.0")
    await send(mon, kyiv(2026, 11, 18, 2, 14, 58), v="230.8", src="GRID")
    assert store.queued() == [] and store.data.power.status is PowerStatus.ON
    assert mon._confirm is None


async def test_a_snapshot_taken_while_the_gx_has_no_data(monitor):
    mon, store, _ = monitor
    store.data.power = PowerState(PowerStatus.ON, kyiv(2026, 11, 17, 22, 16))
    await mon.restore()
    await mon._handle({"id": 1, "type": "event", "event": {"a": {
        mon._config.voltage_entity: {"s": "0.0", "lc": kyiv(2026, 11, 18, 2, 2, 6).timestamp()},
        mon._config.input_source_entity: {"s": "UNKNOWN", "lc": kyiv(2026, 11, 18, 2, 2, 7).timestamp()},
    }}})
    assert store.queued() == [] and store.data.power.status is PowerStatus.ON
    assert mon._confirm is None


async def test_0v_is_an_outage_after_a_while_when_the_input_never_says(powered):
    mon, store, _ = powered
    mon.CONFIRM_S = 0.01
    went_off = kyiv(2026, 11, 17, 8, 5, 31)
    await send(mon, went_off, v="0.0")
    assert store.data.power.status is PowerStatus.ON
    await asyncio.sleep(0.05)
    assert store.data.power == PowerState(PowerStatus.OFF, went_off) and len(store.queued()) == 2


async def test_the_input_turning_unknown_stops_the_wait(powered):
    mon, store, _ = powered
    mon.CONFIRM_S = 0.01
    await send(mon, kyiv(2026, 11, 18, 2, 2, 6), v="0.0")
    await send(mon, src="UNKNOWN")
    await asyncio.sleep(0.05)
    assert store.queued() == [] and store.data.power.status is PowerStatus.ON


async def test_without_an_input_entity_the_voltage_alone_decides(settings):
    store = MemoryStore()
    store.data.power = PowerState(PowerStatus.ON, kyiv(2026, 11, 17, 22, 16))
    config = load_settings({**BASE_ENV, "HA_INPUT_SOURCE_ENTITY": "none"}).power
    assert config.input_source_entity is None
    mon = PowerMonitor(config, store, Router(settings), snapshot=lambda: None, clock=lambda: kyiv(2026, 11, 18, 3))
    await mon.restore()
    went_off = kyiv(2026, 11, 18, 2, 2, 6)
    await mon._handle({"id": 1, "type": "event", "event": {"c": {
        config.voltage_entity: {"+": {"s": "0.0", "lc": went_off.timestamp()}}}}})
    assert store.data.power == PowerState(PowerStatus.OFF, went_off)
