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
from app.notify.router import Router
from app.power.state import PowerStatus, PowerTransition
from app.storage.memory import MemoryStore
from tests.conftest import BASE_ENV, kyiv, one
from tests.test_collector import DAY_OCT1, DAY_OCT2, FakeSite, fact, hours, texts

PUBLISHED = "📅 <b>Tomorrow's schedule published</b> (02.10)\n\n🔴 08:00 – 10:00"


class FakeEdge(Edge):
    """The real Edge with the reserve's answers in place of HTTP."""

    def __init__(self):
        super().__init__("https://edge.test", "key", {"lightbot": "1:a", "schedulebot": "2:b"})
        self.calls = []
        self.up = True
        self.reserve = None     # the baseline the reserve went on with, handed over once

    async def _post(self, source, action, body):
        self.calls.append((action, source, copy.deepcopy(body)))
        if not self.up:
            return None
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


def reserve_reads(site, push):
    """The reserve in charge: each read goes on from the baseline and the days shown of the one before."""
    state = {"baseline": push["baseline"], "shown": []}

    def read(now):
        reply = reserve_read(site, dict(push, baseline=state["baseline"]), now, shown=state["shown"])
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


async def test_failed_cycle_does_not_push(pipeline):
    collector, site, _, _, edge = pipeline
    site.fail = True
    assert not await collector.cycle()
    assert edge.calls == []


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
