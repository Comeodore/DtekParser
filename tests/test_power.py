import pytest

from app.notify.router import Router
from app.power.monitor import PowerMonitor
from app.power.state import PowerState, PowerStatus, next_state, parse_voltage
from app.storage.memory import MemoryStore
from tests.conftest import kyiv, one, snapshot


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
    v, b = mon._config.voltage_entity, mon._config.battery_entity

    # Initial snapshot on subscribe: power is on, nothing to report.
    await mon._handle({"id": 1, "type": "event", "event": {"a": {
        v: {"s": "231.8", "lc": kyiv(2026, 10, 1, 15).timestamp()},
        b: {"s": "100.0", "lc": kyiv(2026, 10, 1, 8).timestamp()},
    }}})
    assert store.queued() == []

    # Voltage noise does not touch the database or produce messages.
    await mon._handle({"id": 1, "type": "event", "event": {"c": {v: {"+": {"s": "232.8", "lc": 1.0}}}}})
    assert store.queued() == []

    went_off = kyiv(2026, 10, 1, 16, 0, 30)
    await mon._handle({"id": 1, "type": "event", "event": {"c": {v: {"+": {"s": "0.0", "lc": went_off.timestamp()}}}}})
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


async def test_empty_database_is_initialised_silently(monitor):
    mon, store, _ = monitor
    await mon.restore()
    v = mon._config.voltage_entity
    await mon._handle({"id": 1, "type": "event", "event": {"a": {v: {"s": "0.0", "lc": kyiv(2026, 10, 1, 9).timestamp()}}}})
    assert store.queued() == []
    assert store.data.power.status is PowerStatus.OFF
