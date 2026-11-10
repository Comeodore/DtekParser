from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.config import ConfigError, load_settings
from app.energy import EnergyConfig, EnergyFeed, EnergyReading, load_energy
from app.notify.router import Router
from app.power.monitor import PowerMonitor
from app.power.state import PowerState, PowerStatus
from app.storage.memory import MemoryStore
from tests.conftest import BASE_ENV, kyiv, one, snapshot
from tests.test_edge import FakeEdge, ms

TOKEN = "secret-token"


def test_reading_payload():
    at = kyiv(2026, 11, 10, 14, 3, 12)
    assert EnergyReading(True, 228.44, at).payload() == {"power": True, "voltage": 228.4,
                                                         "ts": "2026-11-10T14:03:12+02:00"}
    # Kyiv's own offset whatever the stored zone, summer time too.
    summer = datetime(2026, 7, 1, 9, 0, 0, 900000, tzinfo=timezone.utc)
    assert EnergyReading(False, 0.0, summer).payload() == {"power": False, "voltage": 0.0,
                                                           "ts": "2026-07-01T12:00:00+03:00"}
    # A voltage the site would refuse the whole request over is left out.
    for voltage in (None, -1.0, 500.5, float("nan")):
        assert EnergyReading(True, voltage, at).payload() == {"power": True, "ts": "2026-11-10T14:03:12+02:00"}


@pytest.mark.parametrize("extra, message", [
    ({"ENERGY_URL": "https://e.example/api/inputs/2"}, "go together"),
    ({"ENERGY_TOKEN": "t"}, "go together"),
    ({"ENERGY_URL": "e.example/api/inputs/2", "ENERGY_TOKEN": "t"}, "not an http"),
    ({"ENERGY_URL": "https://e.example/api/inputs/2", "ENERGY_TOKEN": "t", "ENERGY_REPEAT_S": "5"}, "at least 30"),
    ({"ENERGY_URL": "https://e.example/api/inputs/2", "ENERGY_TOKEN": "t", "ENERGY_REPEAT_S": "x"}, "not a number"),
])
def test_bad_energy_settings(extra, message):
    env = {**BASE_ENV, **extra}
    with pytest.raises(ConfigError, match=message):
        load_energy(env, load_settings(env))


def test_energy_settings():
    env = {**BASE_ENV, "ENERGY_URL": "https://e.example/api/inputs/2", "ENERGY_TOKEN": "t"}
    assert load_energy(env, load_settings(env)) == EnergyConfig("https://e.example/api/inputs/2", "t", 300.0)
    assert load_energy(BASE_ENV, load_settings(BASE_ENV)) is None
    without_ha = {k: v for k, v in env.items() if not k.startswith("HA_")}
    with pytest.raises(ConfigError, match="Home Assistant"):
        load_energy(without_ha, load_settings(without_ha))


# ---- the power monitor queues the changes ----

@pytest.fixture
def monitor(settings):
    store = MemoryStore()
    snap = snapshot([one(kyiv(2026, 10, 1).date(), [16, 17, 18])])
    clock = {"now": kyiv(2026, 10, 1, 16, 1)}
    noted = []
    mon = PowerMonitor(settings.power, store, Router(settings), snapshot=lambda: snap, clock=lambda: clock["now"],
                       energy=True, on_energy=lambda: noted.append(clock["now"]))
    return mon, store, clock, noted


def change(mon, value, at=None):
    plus = {"s": value}
    if at is not None:
        plus["lc"] = at.timestamp()
    return mon._handle({"id": 1, "type": "event", "event": {"c": {mon._config.voltage_entity: {"+": plus}}}})


async def test_a_change_of_the_power_is_queued_with_its_time_and_voltage(monitor):
    mon, store, clock, noted = monitor
    store.data.power = PowerState(PowerStatus.ON, kyiv(2026, 9, 30, 2, 50))
    await mon.restore()
    await mon._handle({"id": 1, "type": "event", "event": {"a": {
        mon._config.voltage_entity: {"s": "231.8", "lc": kyiv(2026, 10, 1, 15).timestamp()}}}})
    # The same power and the voltage's jitter queue nothing.
    await change(mon, "232.8")
    await change(mon, "unavailable")
    assert store.energy_rows() == [] and noted == []

    went_off = kyiv(2026, 10, 1, 16, 0, 30)
    await change(mon, "0.0", went_off)
    assert [r["reading"] for r in store.energy_rows()] == [EnergyReading(False, 0.0, went_off)]
    assert noted == [clock["now"]]
    # In the same transaction as the power state and the messages: a failed commit leaves none of them.
    store.fail_next_commit = True
    with pytest.raises(ConnectionError):
        await change(mon, "229.5", kyiv(2026, 10, 1, 16, 2))
    assert len(store.energy_rows()) == 1 and store.data.power.status is PowerStatus.OFF


async def test_the_first_reading_ever_is_queued_too(monitor):
    mon, store, _, noted = monitor
    await mon.restore()
    await mon._handle({"id": 1, "type": "event", "event": {"a": {
        mon._config.voltage_entity: {"s": "0.0", "lc": kyiv(2026, 10, 1, 9).timestamp()}}}})
    assert [r["reading"] for r in store.energy_rows()] == [EnergyReading(False, 0.0, kyiv(2026, 10, 1, 9))]
    assert store.queued() == [] and len(noted) == 1


async def test_the_power_now_only_while_home_assistant_says_it(monitor):
    mon, store, clock, _ = monitor
    now = clock["now"]
    await mon.restore()
    assert mon.energy_reading(now) is None
    mon.connected = True
    assert mon.energy_reading(now) is None      # subscribed, no state yet
    await mon._handle({"id": 1, "type": "event", "event": {"a": {
        mon._config.voltage_entity: {"s": "230.1", "lc": kyiv(2026, 10, 1, 9).timestamp()}}}})
    await change(mon, "231.0")
    assert mon.energy_reading(now) == EnergyReading(True, 231.0, now)
    await change(mon, "unavailable")
    assert mon.energy_reading(now) is None
    await change(mon, "230.0")
    mon._disconnected()
    assert mon.energy_reading(now) is None and mon.voltage is None


# ---- the feed ----

class Site:
    """energy.win as far as the feed sees it: replies queued in order, 200 with the count when none is left."""

    def __init__(self):
        self.calls: list[dict] = []
        self.replies: list[tuple[int, object, dict]] = []
        self.server: TestServer = None

    async def handler(self, request):
        body = await request.json()
        self.calls.append({"auth": request.headers.get("Authorization"), "body": body})
        status, reply, headers = self.replies.pop(0) if self.replies else (200, None, {})
        if status == 200 and reply is None:
            reply = {"ok": True, "received": len(body)}
        if reply is None:
            return web.Response(status=status, headers=headers)
        return web.json_response(reply, status=status, headers=headers)

    @property
    def url(self) -> str:
        return str(self.server.make_url("/api/inputs/2"))


@pytest.fixture
async def site():
    s = Site()
    app = web.Application()
    app.router.add_post("/api/inputs/2", s.handler)
    s.server = TestServer(app)
    await s.server.start_server()
    yield s
    await s.server.close()


class Clocks:
    def __init__(self):
        self.now = kyiv(2026, 11, 10, 12, 0)
        self.mono = 1000.0
        self.slept: list[float] = []

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)


def make_feed(url, store, clocks, current=lambda now: None, repeat_s=300.0):
    return EnergyFeed(EnergyConfig(url, TOKEN, repeat_s), store, current, clock=lambda: clocks.now,
                      monotonic=lambda: clocks.mono, sleep=clocks.sleep)


async def queue(store, *readings):
    async with store.transaction() as tx:
        await tx.enqueue_energy(list(readings))


def pending(store):
    return [r["reading"] for r in store.energy_rows() if r["sent_at"] is None and r["failed_at"] is None]


ON = EnergyReading(True, 228.4, kyiv(2026, 11, 10, 11, 58))
OFF = EnergyReading(False, 0.0, kyiv(2026, 11, 10, 11, 59))


async def test_sends_what_waits_as_one_array_with_the_token(site):
    store, clocks = MemoryStore(), Clocks()
    feed = make_feed(site.url, store, clocks)
    await queue(store, ON, OFF)
    assert await feed.flush() == 2
    assert site.calls == [{"auth": f"Bearer {TOKEN}", "body": [ON.payload(), OFF.payload()]}]
    assert pending(store) == [] and feed.sent == 2 and feed.last_success == clocks.now
    assert await feed.flush() == 0 and len(site.calls) == 1
    await feed.close()


async def test_a_backlog_goes_in_arrays_of_500_two_seconds_apart(site):
    store, clocks = MemoryStore(), Clocks()
    feed = make_feed(site.url, store, clocks)
    await queue(store, *[EnergyReading(i % 2 == 0, 230.0, kyiv(2026, 11, 9) + timedelta(minutes=5 * i))
                         for i in range(1001)])
    assert await feed.flush() == 1001
    assert [len(c["body"]) for c in site.calls] == [500, 500, 1]
    assert site.calls[0]["body"][0]["ts"] == "2026-11-09T00:00:00+02:00"
    assert clocks.slept == [2.0, 2.0]
    await feed.close()


async def test_refused_readings_are_dropped_with_the_reason(site):
    store, clocks = MemoryStore(), Clocks()
    feed = make_feed(site.url, store, clocks)
    await queue(store, ON)
    site.replies.append((400, {"statusCode": 400, "message": "Поле power обов’язкове"}, {}))
    assert await feed.flush() == 0
    assert pending(store) == [] and store.energy_rows()[0]["error"] == "Поле power обов’язкове"
    assert feed.rejected == 1 and feed.last_error == "HTTP 400: Поле power обов’язкове"
    # Not a reason to stop: what comes next goes out.
    await queue(store, OFF)
    assert await feed.flush() == 1 and feed.last_error is None
    await feed.close()


@pytest.mark.parametrize("reply, wait", [
    ((503, {"message": "down"}, {}), 5.0),
    ((429, None, {"Retry-After": "42"}), 42.0),
    ((429, None, {}), 60.0),
    ((401, None, {}), 600.0),
])
async def test_a_failure_keeps_the_readings_until_it_is_time_to_try_again(site, reply, wait):
    store, clocks = MemoryStore(), Clocks()
    feed = make_feed(site.url, store, clocks)
    await queue(store, ON)
    site.replies.append(reply)
    assert await feed.flush() == 0
    assert pending(store) == [ON] and feed.failures == 1 and feed.status()["retry_in_s"] == wait
    clocks.advance(wait - 1)
    assert await feed.flush() == 0 and len(site.calls) == 1
    clocks.advance(1)
    assert await feed.flush() == 1 and pending(store) == []
    assert feed.status()["retry_in_s"] is None and feed.last_error is None
    await feed.close()


async def test_an_unreachable_site_backs_off_up_to_five_minutes():
    store, clocks = MemoryStore(), Clocks()
    feed = make_feed("http://127.0.0.1:1/api/inputs/2", store, clocks)
    await queue(store, ON)
    waits = []
    for _ in range(8):
        await feed.flush()
        waits.append(feed.status()["retry_in_s"])
        clocks.advance(waits[-1])
    assert waits == [5, 10, 20, 40, 80, 160, 300, 300]
    assert pending(store) == [ON] and feed.last_error.startswith("ClientConnectorError")
    await feed.close()


async def test_repeats_the_power_now_every_interval_and_not_while_it_is_unknown(site):
    store, clocks = MemoryStore(), Clocks()
    now_reading = {"value": None}
    feed = make_feed(site.url, store, clocks, current=lambda now: now_reading["value"] and
                     EnergyReading(now_reading["value"], 230.0, now))
    assert not await feed.report_if_due()           # HA has not said it yet
    now_reading["value"] = True
    assert await feed.report_if_due()
    assert pending(store) == [EnergyReading(True, 230.0, clocks.now)]
    clocks.advance(299)
    assert not await feed.report_if_due()
    clocks.advance(1)
    assert await feed.report_if_due()
    # A change queued by the power monitor counts as a reading: the repeat is a full interval after it.
    clocks.advance(200)
    feed.noted()
    clocks.advance(299)
    assert not await feed.report_if_due()
    assert len(pending(store)) == 2
    await feed.close()


async def test_old_readings_it_never_took_expire_and_sent_ones_are_purged():
    store, clocks = MemoryStore(), Clocks()
    feed = make_feed("http://127.0.0.1:1/api/inputs/2", store, clocks)
    await queue(store, ON, OFF)
    ids = sorted(store.data.energy)
    store.data.energy[ids[0]]["created_at"] = clocks.now - timedelta(days=8)
    store.data.energy[ids[1]]["sent_at"] = clocks.now - timedelta(days=8)
    await feed._purge_if_due()
    # The sent one is gone; the expired one stays a week for a look, as failed.
    assert [r["error"] for r in store.energy_rows()] == ["expired"] and pending(store) == []
    await feed.close()


def test_metrics_and_health_show_the_queue():
    feed = EnergyFeed(EnergyConfig("https://e.example", TOKEN), MemoryStore(), lambda now: None)
    feed.pending, feed.sent, feed.last_success = 3, 7, kyiv(2026, 11, 10, 12, 0)
    text = feed.exposition()
    assert "energy_feed_pending 3\n" in text and "energy_feed_sent_total 7\n" in text
    assert f"energy_feed_last_success_timestamp_seconds {kyiv(2026, 11, 10, 12).timestamp()}" in text
    assert TOKEN not in text and TOKEN not in str(feed.status())
    assert feed.status()["pending"] == 3


# ---- what the reserve saw while the service was away ----

async def test_a_change_the_reserve_saw_meanwhile_is_queued_on_the_take_back():
    store, edge = MemoryStore(), FakeEdge()
    async with store.transaction() as tx:
        await tx.save_power_state(PowerState(PowerStatus.ON, kyiv(2026, 10, 1, 7, 30)), 1)
        await tx.save_power_state(PowerState(PowerStatus.ON, kyiv(2026, 10, 1, 6, 0)), 2)
    edge.power_left = {"kem": {"on": False, "since": ms(kyiv(2026, 10, 1, 19, 1))},
                       "krem": {"on": False, "since": ms(kyiv(2026, 10, 1, 19, 2))}}
    await edge.take_back_power(store, [("kem", 1), ("krem", 2)], energy_slot=1)
    assert [r["reading"] for r in store.energy_rows()] == [EnergyReading(False, None, kyiv(2026, 10, 1, 19, 1))]
    # No change, nothing queued.
    await edge.take_back_power(store, [("kem", 1)], energy_slot=1)
    assert len(store.energy_rows()) == 1


async def test_the_service_runs_the_feed_only_when_configured(settings):
    from app.service import Service
    assert Service(settings, store=MemoryStore(), gateway=object()).energy is None
    service = Service(settings, store=MemoryStore(), gateway=object(),
                      energy=EnergyConfig("https://e.example/api/inputs/2", TOKEN))
    assert service.energy is not None and service.power._energy
    service.power._state = PowerState(PowerStatus.ON, kyiv(2026, 10, 1))
    service.power.connected, service.power.voltage = True, 229.0
    assert service.energy._current(kyiv(2026, 10, 1, 12)) == EnergyReading(True, 229.0, kyiv(2026, 10, 1, 12))
