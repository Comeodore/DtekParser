"""Taking turns with the reserve on Cloudflare: pushes, claiming a source back, and its dtek-brain."""
import copy
from datetime import date, timedelta

import pytest

from app import brain
from app.collector import SourceCollector
from app.config import ConfigError, load_settings
from app.dtek.models import Snapshot
from app.edge import Edge
from app.engine.events import ScheduleChange, ScheduleEvent
from app.inverter.reading import parse_device_latest
from app.notify.messages import render_power
from app.notify.router import Router
from app.power.state import PowerState, PowerStatus, PowerTransition
from app.storage.memory import MemoryStore
from tests.conftest import BASE_ENV, kyiv, one
from tests.test_collector import DAY_OCT1, DAY_OCT2, FakeSite, fact, hours, texts
from tests.test_inverter import device

PUBLISHED = "📅 <b>Tomorrow's schedule published</b> (02.10)\n\n🔴 08:00 – 10:00"


class FakeEdge(Edge):
    """The real Edge with the reserve's answers in place of HTTP."""

    def __init__(self):
        super().__init__("https://edge.test", "key", {"lightbot": "1:a", "schedulebot": "2:b"})
        self.calls = []
        self.up = True
        self.reserve = None     # the baseline the reserve went on with, handed over once
        self.power_left = {}    # per source, the power state the reserve left, handed over once

    async def _post(self, source, action, body):
        self.calls.append((action, source, copy.deepcopy(body)))
        if not self.up:
            return None
        if action == "claim" and body.get("power"):
            return {"power": self.power_left.pop(source, None)}
        if action == "claim":
            baseline, self.reserve = self.reserve, None
            return {"baseline": baseline}
        return {"ok": True}

    def actions(self):
        return [action for action, _, _ in self.calls]


@pytest.fixture
def pipeline(settings):
    store, site, clock, edge = MemoryStore(), FakeSite(), {"now": kyiv(2026, 10, 1, 18)}, FakeEdge()
    collector = SourceCollector(settings.source("kem"), site, store, Router(settings), clock=lambda: clock["now"],
                                edge=edge)
    return collector, site, store, clock, edge


def reserve_read(site, push, now, **extra):
    """One read of the reserve, as worker.js hands it to dtek-brain."""
    return brain.process({
        "source": "kem", "now": int(now.timestamp() * 1000), "building": "43",
        "answer": copy.deepcopy(site.answer), "fact": copy.deepcopy(site.fact), "names": {"GPV4.1": "Черга 4.1"},
        "baseline": push["baseline"], "fingerprint": push["fingerprint"],
        "lang": push["lang"], "notify_outages": push["notify_outages"], **extra,
    })


def reserve_reads(site, push, **extra):
    """The reserve in charge: each read goes on from the baseline and the days shown of the one before."""
    state = {"baseline": push["baseline"], "shown": []}

    def read(now):
        reply = reserve_read(site, dict(push, baseline=state["baseline"]), now, shown=state["shown"], **extra)
        state.update(baseline=reply["baseline"], shown=reply["shown"])
        return reply
    return read


async def test_every_good_cycle_pushes_the_baseline_bot_and_chats(pipeline):
    collector, _, store, clock, edge = pipeline
    await collector.cycle()
    clock["now"] = kyiv(2026, 10, 1, 18, 1)
    await collector.cycle()
    # Claimed once at the start, then only pushes while it keeps pushing.
    assert edge.actions() == ["claim", "home", "home"]
    push = edge.calls[-1][2]
    assert push["baseline"] == store.data.sources["kem"].baseline
    assert (push["token"], push["chat_ids"], push["lang"], push["notify_outages"]) == ("1:a", ["111", "222"], "en", True)
    assert push["fingerprint"] == collector.config.fingerprint
    # No power state held for the source, none pushed.
    assert "power" not in push


async def test_failed_cycle_only_says_it_is_alive(pipeline):
    collector, site, _, clock, edge = pipeline
    site.fail = True
    assert not await collector.cycle()
    # No news of DTEK to push, but the power is still watched here; once a minute at most.
    assert edge.calls == [("home", "kem", {"alive": True})]
    clock["now"] += timedelta(seconds=30)
    await collector.cycle()
    assert len(edge.calls) == 1
    clock["now"] += timedelta(seconds=31)
    await collector.cycle()
    assert edge.actions() == ["home", "home"]
    # A good cycle claims and pushes as before, and counts as heard from.
    site.fail = False
    await collector.cycle()
    assert edge.actions()[-2:] == ["claim", "home"] and "baseline" in edge.calls[-1][2]
    site.fail = True
    clock["now"] += timedelta(seconds=30)
    await collector.cycle()
    assert len(edge.calls) == 4


async def test_pushes_carry_the_power_state_the_reserve_goes_on_from(pipeline):
    collector, site, _, clock, edge = pipeline
    state = PowerState(PowerStatus.OFF, kyiv(2026, 10, 1, 17, 5))
    edge.power = lambda source: state if source == "kem" else None
    await collector.cycle()
    assert edge.calls[-1][2]["power"] == {"on": False, "since": ms(kyiv(2026, 10, 1, 17, 5))}
    site.fail = True
    clock["now"] += timedelta(minutes=2)
    await collector.cycle()
    assert edge.calls[-1][2] == {"alive": True, "power": {"on": False, "since": ms(kyiv(2026, 10, 1, 17, 5))}}


async def test_a_start_takes_back_the_power_the_reserve_watched():
    store, edge = MemoryStore(), FakeEdge()
    async with store.transaction() as tx:
        await tx.save_power_state(PowerState(PowerStatus.ON, kyiv(2026, 10, 1, 7, 30)), 1)
        await tx.save_power_state(PowerState(PowerStatus.ON, kyiv(2026, 10, 1, 6, 0)), 2)
    # The reserve saw kem's power go at 19:01 while the service was away, and watched krem's without a change.
    edge.power_left = {"kem": {"on": False, "since": ms(kyiv(2026, 10, 1, 19, 1))}}
    await edge.take_back_power(store, [("kem", 1), ("krem", 2)])
    assert [(a, s, b) for a, s, b in edge.calls] == [("claim", "kem", {"power": True}), ("claim", "krem", {"power": True})]
    async with store.transaction() as tx:
        assert await tx.load_power_state(1) == PowerState(PowerStatus.OFF, kyiv(2026, 10, 1, 19, 1))
        assert await tx.load_power_state(2) == PowerState(PowerStatus.ON, kyiv(2026, 10, 1, 6, 0))
    # A reserve out of reach leaves the stored states as they were.
    edge.up, edge.power_left = False, {"kem": {"on": True, "since": ms(kyiv(2026, 10, 1, 20, 0))}}
    await edge.take_back_power(store, [("kem", 1)])
    async with store.transaction() as tx:
        assert await tx.load_power_state(1) == PowerState(PowerStatus.OFF, kyiv(2026, 10, 1, 19, 1))


async def test_back_after_the_reserve_took_over_sends_nothing_twice(pipeline):
    collector, site, store, clock, edge = pipeline
    await collector.cycle()
    last_push = edge.calls[-1][2]
    # The service goes away; tomorrow is published and the reserve sends it.
    site.fact = fact("01.10.2026 19:40", {DAY_OCT1: {"GPV4.1": hours()}, DAY_OCT2: {"GPV4.1": hours(8, 9)}})
    reserve = reserve_read(site, last_push, kyiv(2026, 10, 1, 19, 45))
    assert reserve["texts"] == [PUBLISHED]
    assert reserve["events"] == ["schedule 2026-10-02 published"]
    edge.reserve = reserve["baseline"]
    # Back: it claims the source and goes on from the reserve's baseline, so the news is not repeated.
    clock["now"] = kyiv(2026, 10, 1, 20, 30)
    await collector.cycle()
    assert edge.actions()[-2:] == ["claim", "home"]
    assert texts(store) == []
    # Later news is reported as usual.
    site.fact = fact("01.10.2026 20:40", {DAY_OCT1: {"GPV4.1": hours()}, DAY_OCT2: {"GPV4.1": hours(8)}})
    clock["now"] = kyiv(2026, 10, 1, 20, 41)
    await collector.cycle()
    assert texts(store) == ["📅 <b>Tomorrow's schedule updated</b> (02.10)\n\n🔴 08:00 – 09:00"]


async def test_back_while_the_reserve_stayed_idle_reports_the_difference(pipeline):
    collector, site, store, clock, edge = pipeline
    await collector.cycle()
    site.fact = fact("01.10.2026 19:40", {DAY_OCT1: {"GPV4.1": hours()}, DAY_OCT2: {"GPV4.1": hours(8, 9)}})
    clock["now"] = kyiv(2026, 10, 1, 20, 30)
    await collector.cycle()
    assert edge.actions()[-2:] == ["claim", "home"]
    assert texts(store) == [PUBLISHED]


async def test_unreachable_reserve_leaves_the_service_as_it_was(pipeline):
    collector, site, store, clock, edge = pipeline
    edge.up = False
    await collector.cycle()
    site.fact = fact("01.10.2026 19:40", {DAY_OCT1: {"GPV4.1": hours()}, DAY_OCT2: {"GPV4.1": hours(8, 9)}})
    clock["now"] = kyiv(2026, 10, 1, 19, 41)
    await collector.cycle()
    assert texts(store) == [PUBLISHED]
    # No push got through, so it keeps asking before each cycle.
    assert edge.actions() == ["claim", "home", "claim", "home"]


async def test_brain_initialises_without_a_baseline():
    site = FakeSite()
    reply = reserve_read(site, {"baseline": None, "fingerprint": "f", "lang": "en", "notify_outages": True},
                         kyiv(2026, 10, 1, 18))
    assert reply["initialized"] and reply["texts"] == [] and reply["baseline"]["fingerprint"] == "f"
    # Without the days shown the worker keeps, it sends only what changed, as before.
    assert "shown" not in reply


async def test_reserve_in_charge_shows_the_schedule_once(pipeline):
    collector, site, _, _, edge = pipeline
    site.fact = fact("01.10.2026 06:50", {DAY_OCT1: {"GPV4.1": hours(19, 20)}})
    await collector.cycle()
    read = reserve_reads(site, edge.calls[-1][2])
    # Taken over with nothing changed: today as it is, since the mini app is down with the service.
    assert read(kyiv(2026, 10, 1, 18, 10))["texts"] == ["📅 <b>Today's schedule</b> (01.10)\n\n🔴 19:00 – 21:00"]
    # Then nothing until it changes; hours going by are no news.
    assert read(kyiv(2026, 10, 1, 18, 15))["texts"] == []
    assert read(kyiv(2026, 10, 1, 20, 15))["texts"] == []


async def test_reserve_sends_a_day_with_news_once(pipeline):
    collector, site, _, _, edge = pipeline
    await collector.cycle()
    read = reserve_reads(site, edge.calls[-1][2])
    assert read(kyiv(2026, 10, 1, 18, 10))["texts"] == ["📅 <b>Today's schedule</b> (01.10)\n\n✅ No more outages today"]
    site.fact = fact("01.10.2026 19:40", {DAY_OCT1: {"GPV4.1": hours()}, DAY_OCT2: {"GPV4.1": hours(8, 9)}})
    reply = read(kyiv(2026, 10, 1, 19, 45))
    assert reply["texts"] == [PUBLISHED]
    assert [d["date"] for d in reply["shown"]] == ["2026-10-01", "2026-10-02"]
    # Past midnight tomorrow is today, and was shown already.
    assert read(kyiv(2026, 10, 2, 0, 5))["texts"] == []


async def test_reserve_shows_tomorrow_published_without_outages(pipeline):
    collector, site, _, _, edge = pipeline
    await collector.cycle()
    read = reserve_reads(site, edge.calls[-1][2])
    read(kyiv(2026, 10, 1, 18, 10))
    site.fact = fact("01.10.2026 19:40", {DAY_OCT1: {"GPV4.1": hours()}, DAY_OCT2: {"GPV4.1": hours()}})
    reply = read(kyiv(2026, 10, 1, 19, 45))
    # Not news to the detector, but the mini app would have shown it.
    assert reply["events"] == []
    assert reply["texts"] == ["📅 <b>Tomorrow's schedule</b> (02.10)\n\n✅ No outages planned"]


async def test_reserve_shows_the_schedule_once_it_applies_again(pipeline):
    """04.11.2026 at kem: DTEK published the day during an emergency without schedules, which then became a
    stabilisation outage by the schedule; nothing was sent, and the mini app was down."""
    collector, site, _, _, edge = pipeline
    house = site.answer["data"]["43"]
    house.update(type="2", start_date="11:35 30.09.2026", end_date="20:00 01.10.2026",
                 sub_type="Екстренні відключення (Аварійне без застосування графіку погодинних відключень)")
    await collector.cycle()
    read = reserve_reads(site, edge.calls[-1][2])
    site.fact = fact("01.10.2026 19:40", {DAY_OCT1: {"GPV4.1": hours(21, 22)}})
    # While schedules do not apply, none is sent.
    assert read(kyiv(2026, 10, 1, 19, 45))["texts"] == []
    house.update(start_date="20:23 01.10.2026", end_date="23:00 01.10.2026",
                 sub_type="Стабілізаційне відключення (Згідно графіку погодинних відключень)")
    reply = read(kyiv(2026, 10, 1, 20, 30))
    assert reply["events"] == ["outage started"]
    assert reply["texts"] == ["📅 <b>Today's schedule</b> (01.10)\n\n🔴 21:00 – 23:00"]


def test_dtek_news_expires_sooner_than_power_messages(settings):
    router, now = Router(settings), kyiv(2026, 10, 1, 18)
    snapshot = Snapshot("kem", now, ("GPV4.1",), (one(date(2026, 10, 2), off=[8]),), None, "", "")
    event = ScheduleEvent(date(2026, 10, 2), ScheduleChange.PUBLISHED, snapshot.days[0], None)
    news = router.for_events(settings.source("kem"), [event], snapshot, now)
    power = router.for_power(PowerTransition(PowerStatus.OFF, now, now - timedelta(hours=1), "80%", False),
                             snapshot, now)
    assert {m.expires_at for m in news} == {now + timedelta(minutes=15)}
    assert {m.expires_at for m in power} == {now + timedelta(hours=6)}


def test_edge_url_and_key_go_together():
    with pytest.raises(ConfigError):
        load_settings(dict(BASE_ENV, EDGE_URL="https://edge.test"))
    settings = load_settings(dict(BASE_ENV, EDGE_URL="https://edge.test/", EDGE_KEY="k"))
    assert (settings.edge_url, settings.edge_key) == ("https://edge.test", "k")


def reserve_page(site, now, read_at=None, **extra):
    """The mini app's data as worker.js asks dtek-brain for it while the home service is away."""
    return brain.page({
        "source": "kem", "now": int(now.timestamp() * 1000), "building": "43",
        "answer": copy.deepcopy(site.answer), "fact": copy.deepcopy(site.fact), "names": {"GPV4.1": "Черга 4.1"},
        "read_at": int((read_at or now).timestamp() * 1000), "lang": "en", **extra,
    })


def ms(at):
    return int(at.timestamp() * 1000)


def test_reserve_page_shows_the_last_read_and_the_victron():
    site = FakeSite()
    site.fact = fact("01.10.2026 06:50", {DAY_OCT1: {"GPV4.1": hours(19, 20)}, DAY_OCT2: {"GPV4.1": hours(8)}})
    now = kyiv(2026, 10, 1, 19, 30)
    victron = {"grid": False, "grid_since": ms(kyiv(2026, 10, 1, 19, 2)), "grid_at": ms(now) - 40_000,
               "soc": 71.5, "battery_w": -420, "battery_at": ms(now) - 40_000}
    page = reserve_page(site, now, read_at=kyiv(2026, 10, 1, 19, 27), victron=victron)
    assert page["reserve"] is True and page["healthy"] is True and page["source"] == "kem"
    assert page["updated_at"].startswith("2026-10-01T19:27")
    assert [d["date"] for d in page["days"]] == ["2026-10-01", "2026-10-02"]
    assert page["scheduled_status"]["state"] == "off"
    assert (page["power"]["state"], page["power"]["since"], page["power"]["source"]) == ("off", "19:02", "vrm")
    assert page["power"]["expected"]["state"] == "off"
    assert page["batteries"] == [{"name": "home", "soc": 71.5, "battery_w": -420, "stale": False}]


def test_reserve_page_without_fresh_data_says_so():
    site = FakeSite()
    now = kyiv(2026, 10, 1, 19, 30)
    old = ms(now) - 20 * 60_000
    victron = {"grid": True, "grid_since": None, "grid_at": old, "soc": 40, "battery_w": 0, "battery_at": old}
    page = reserve_page(site, now, read_at=kyiv(2026, 10, 1, 19, 10), victron=victron)
    # A read 20 minutes old may be out of date; a grid state that old is not the power there.
    assert page["healthy"] is False
    assert page["power"] is None
    assert page["batteries"] == [{"name": "home", "soc": 40, "battery_w": 0, "stale": True}]


def deye_item(sn, at, **points):
    """One inverter of device/latest, as dtek-edge passes it on: the fixture's points with these changed."""
    item = device(**points)
    item.update(deviceSn=sn, collectionTime=str(int(at.timestamp())))
    return item


def test_reserve_page_falls_back_on_the_building_inverters():
    site = FakeSite()
    now = kyiv(2026, 10, 1, 19, 30)
    lift = deye_item("L", now - timedelta(minutes=1), SOC="88", BatteryPower="150", GridVoltageL1L2="0")
    water = deye_item("W", now - timedelta(minutes=2), SOC="64", BatteryPower="-900", GridVoltageL1L2="0")
    deye = {"items": [water, lift], "batteries": [["elevator", "L"], ["water", "W"]], "min_grid_voltage": 100}
    page = reserve_page(site, now, deye=deye)
    assert (page["power"]["state"], page["power"]["since"], page["power"]["source"]) == ("off", None, "deye_batteries")
    # In DEYE_BATTERIES' order, with Deye's sign turned: positive while charging.
    assert [(b["name"], b["soc"], b["battery_w"], b["stale"]) for b in page["batteries"]] == [
        ("elevator", 88, -150, False), ("water", 64, 900, False)]
    assert page["inverter"] is None
    # With the Victron there, it comes first and its battery leads the row.
    victron = {"grid": True, "grid_since": ms(now) - 3_600_000, "grid_at": ms(now), "soc": 90, "battery_w": 50,
               "battery_at": ms(now)}
    page = reserve_page(site, now, deye=deye, victron=victron)
    assert (page["power"]["state"], page["power"]["source"]) == ("on", "vrm")
    assert [b["name"] for b in page["batteries"]] == ["home", "elevator", "water"]
    # Inverters that disagree say nothing about the grid.
    deye["items"][0] = deye_item("W", now, SOC="64", GridVoltageL1L2="231")
    assert reserve_page(site, now, deye=deye)["power"] is None


def test_reserve_page_draws_the_krem_inverter():
    site = FakeSite()
    now = kyiv(2026, 10, 16, 17, 25)
    deye = {"items": [deye_item("K", kyiv(2026, 10, 16, 17, 22))], "main": "K", "min_grid_voltage": 100}
    page = reserve_page(site, now, deye=deye)
    inverter = page["inverter"]
    # As the home service's diagram, from the same reading.
    assert inverter == dict(parse_device_latest(deye["items"][0], 100).to_dict(now, 15 * 60),
                            grid_night_kwh=None, grid_day_kwh=None, pv_forecast_kwh=None, battery_until=None)
    assert inverter["time"] == "17:22" and inverter["stale"] is False and inverter["grid_present"] is True
    assert page["batteries"] == [] and page["power"] is None
    # An old reading is drawn greyed out; an inverter Deye has no data for is not drawn.
    assert reserve_page(site, now + timedelta(minutes=20), deye=deye)["inverter"]["stale"] is True
    assert reserve_page(site, now, deye=dict(deye, main="X"))["inverter"] is None


def test_reserve_page_splits_the_grid_and_shows_the_forecast_the_home_service_pushed():
    site = FakeSite()
    now = kyiv(2026, 10, 16, 23, 40)
    # The counter at 23:00 was 7.6 kWh and is 8.0 now (the fixture's DailyEnergyPurchased).
    deye = {"items": [deye_item("K", kyiv(2026, 10, 16, 23, 38))], "main": "K", "min_grid_voltage": 100}
    pushed = {"grid_day": {"day": "2026-10-16", "marks": {"7": 2.1}, "night_start_h": 23, "night_end_h": 7},
              "sun": {"2026-10-16": 6.4, "2026-10-17": 3.2}}
    own = {"day": "2026-10-16", "marks": {"23": 7.6}}
    inverter = reserve_page(site, now, deye=deye, inverter=pushed, grid_day=own)["inverter"]
    # As at home: the night is the 07:00 counter and what came after 23:00, the day the rest.
    assert (inverter["grid_today_kwh"], inverter["grid_night_kwh"], inverter["grid_day_kwh"]) == (8.0, 2.5, 5.5)
    assert inverter["pv_forecast_kwh"] == 6.4 and inverter["battery_until"] is None
    # The home service's mark wins over one noted here; one of another day is left out.
    both = reserve_page(site, now, deye=deye, inverter=pushed, grid_day={"day": "2026-10-16", "marks": {"7": 1.0, "23": 7.6}})
    assert both["inverter"]["grid_night_kwh"] == 2.5
    stale = reserve_page(site, now, deye=deye, inverter=pushed, grid_day={"day": "2026-10-15", "marks": {"23": 7.6}})
    assert (stale["inverter"]["grid_night_kwh"], stale["inverter"]["grid_day_kwh"]) == (None, None)
    # After midnight, with the home service still away: the next day's forecast, and all of it night until 07:00.
    later = kyiv(2026, 10, 17, 1, 10)
    deye["items"] = [deye_item("K", kyiv(2026, 10, 17, 1, 9), DailyEnergyPurchased="0.4")]
    inverter = reserve_page(site, later, deye=deye, inverter=pushed, grid_day=own)["inverter"]
    assert (inverter["grid_night_kwh"], inverter["grid_day_kwh"], inverter["pv_forecast_kwh"]) == (0.4, 0.0, 3.2)
    # A reading still of the day before is set against that day's forecast, not today's.
    deye["items"] = [deye_item("K", kyiv(2026, 10, 16, 14, 58))]
    inverter = reserve_page(site, later, deye=deye, inverter=pushed, grid_day=own)["inverter"]
    assert (inverter["stale"], inverter["pv_forecast_kwh"]) == (True, 6.4)
    # A day the forecast does not reach, and no push at all: the page shows the total alone, no forecast.
    deye["items"] = [deye_item("K", kyiv(2026, 10, 19, 12, 0))]
    inverter = reserve_page(site, kyiv(2026, 10, 19, 12, 1), deye=deye)["inverter"]
    assert (inverter["grid_night_kwh"], inverter["grid_day_kwh"], inverter["pv_forecast_kwh"]) == (None, None, None)


async def test_the_inverters_source_pushes_its_day_for_the_reserve_page(pipeline):
    collector, _, _, _, edge = pipeline
    day = {"grid_day": {"day": "2026-10-01", "marks": {"7": 2.1}, "night_start_h": 23, "night_end_h": 7},
           "sun": {"2026-10-01": 6.4}}

    async def inverter(source):
        return day if source == "kem" else None
    edge.inverter = inverter
    await collector.cycle()
    assert edge.calls[-1][2]["inverter"] == day

    async def broken(source):
        raise RuntimeError("no")
    # The push goes out without it rather than not at all.
    edge.inverter = broken
    await collector.cycle()
    assert edge.calls[-1][0] == "home" and "inverter" not in edge.calls[-1][2]


async def test_the_batteries_source_pushes_what_the_reserve_reads_deye_with(pipeline):
    collector, _, _, _, edge = pipeline
    access = {"token": "t", "expires_at": 1, "base_url": "https://deye.test", "batteries": [["elevator", "1"]],
              "min_grid_voltage": 100}
    edge.deye = lambda source: access if source == "kem" else None
    await collector.cycle()
    assert edge.calls[-1][2]["deye"] == access
    edge.deye = lambda source: None
    await collector.cycle()
    assert "deye" not in edge.calls[-1][2]


def power_call(site, now, state, on, at, battery="71%", lang="en"):
    """What worker.js asks dtek-brain when the power it watches changes (or at first)."""
    return brain.power({
        "source": "kem", "now": ms(now), "lang": lang, "building": "43",
        "answer": copy.deepcopy(site.answer), "fact": copy.deepcopy(site.fact), "names": {"GPV4.1": "Черга 4.1"},
        "read_at": ms(now), "state": state, "observed": {"on": on, "at": ms(at), "battery": battery},
    })


def test_reserve_power_messages_are_the_home_services():
    site = FakeSite()
    site.fact = fact("01.10.2026 06:50", {DAY_OCT1: {"GPV4.1": hours(19, 20)}})
    # The first observation only sets the state.
    first = power_call(site, kyiv(2026, 10, 1, 18, 0), None, True, kyiv(2026, 10, 1, 7, 30))
    assert first == {"state": {"on": True, "since": ms(kyiv(2026, 10, 1, 7, 30))}, "texts": []}
    assert power_call(site, kyiv(2026, 10, 1, 18, 30), first["state"], True, kyiv(2026, 10, 1, 18, 29))["texts"] == []
    # Gone at 19:01, seen a minute later: worded as at home, with the schedule's end, and not as a late notice.
    off = power_call(site, kyiv(2026, 10, 1, 19, 2), first["state"], False, kyiv(2026, 10, 1, 19, 1))
    transition = PowerTransition(PowerStatus.OFF, kyiv(2026, 10, 1, 19, 1), kyiv(2026, 10, 1, 7, 30), "71%", False)
    snapshot = brain._snapshot({"source": "kem", "building": "43", "answer": site.answer, "fact": site.fact,
                                "names": {"GPV4.1": "Черга 4.1"}}, kyiv(2026, 10, 1, 19, 2))
    assert off["texts"] == [render_power(transition, snapshot, kyiv(2026, 10, 1, 19, 2), "en")]
    assert "21:00" in off["texts"][0]
    assert off["state"] == {"on": False, "since": ms(kyiv(2026, 10, 1, 19, 1))}
    back = power_call(site, kyiv(2026, 10, 1, 20, 47), off["state"], True, kyiv(2026, 10, 1, 20, 46), "38%")
    assert back["texts"][0].startswith("🟢") and "38%" in back["texts"][0] and "1h 45m" in back["texts"][0]


def test_reserve_words_the_power_from_the_pushed_baseline():
    site = FakeSite()
    site.fact = fact("01.10.2026 06:50", {DAY_OCT1: {"GPV4.1": hours(19, 20)}})
    # The home service's baseline as it pushed it, before it went away.
    baseline = reserve_read(site, {"baseline": None, "fingerprint": "f", "lang": "en", "notify_outages": True},
                            kyiv(2026, 10, 1, 18, 0))["baseline"]
    state = {"on": True, "since": ms(kyiv(2026, 10, 1, 7, 30))}
    off = brain.power({"source": "kem", "now": ms(kyiv(2026, 10, 1, 19, 2)), "lang": "en", "building": "43",
                       "baseline": baseline, "read_at": ms(kyiv(2026, 10, 1, 18, 0)), "state": state,
                       "observed": {"on": False, "at": ms(kyiv(2026, 10, 1, 19, 1)), "battery": "71%"}})
    # The same words as from a read of its own: off by the schedule, until 21:00.
    assert off["texts"] == power_call(site, kyiv(2026, 10, 1, 19, 2), state, False, kyiv(2026, 10, 1, 19, 1))["texts"]
    assert "21:00" in off["texts"][0]
    # With neither, the message has no schedule to go by.
    bare = brain.power({"source": "kem", "now": ms(kyiv(2026, 10, 1, 19, 2)), "lang": "en", "state": state,
                        "observed": {"on": False, "at": ms(kyiv(2026, 10, 1, 19, 1)), "battery": "71%"}})
    assert "21:00" not in bare["texts"][0]


def test_home_sends_the_power_messages_with_the_reserve_configured():
    transition = PowerTransition(PowerStatus.OFF, kyiv(2026, 10, 1, 19, 1), kyiv(2026, 10, 1, 7, 30), "71%", False)
    plain = Router(load_settings(BASE_ENV))
    assert len(plain.for_power(transition, None, kyiv(2026, 10, 1, 19, 2))) == 2
    with_reserve = Router(load_settings({**BASE_ENV, "EDGE_URL": "https://edge.test", "EDGE_KEY": "k"}))
    assert len(with_reserve.for_power(transition, None, kyiv(2026, 10, 1, 19, 2))) == 2



def test_the_service_pushes_the_power_it_messages():
    from app.service import Service
    from tests.test_inverter import DEYE_ENV
    env = {**BASE_ENV, **DEYE_ENV, "EDGE_URL": "https://edge.test", "EDGE_KEY": "k"}
    service = Service(load_settings(env), store=MemoryStore(), gateway=object())
    # kem's from Home Assistant (row 1), krem's from its Deye inverter (row 2).
    assert service.power_slots() == [("kem", 1), ("krem", 2)]
    service.power._state = PowerState(PowerStatus.ON, kyiv(2026, 10, 1, 7, 30))
    service.inverter._grid = PowerState(PowerStatus.OFF, kyiv(2026, 10, 1, 6, 0))
    assert service.edge.power("kem") == service.power.state
    assert service.edge.power("krem") == service.inverter.grid
    # An inverter whose grid this service does not message: neither does the reserve.
    quiet = Service(load_settings({**env, "DEYE_NOTIFY_GRID": "false"}), store=MemoryStore(), gateway=object())
    assert quiet.power_slots() == [("kem", 1)] and quiet.edge.power("krem") is None
