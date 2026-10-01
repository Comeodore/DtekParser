"""The whole pipeline (site -> detector -> store -> outbox) with fakes at the edges."""
import copy
import json

import pytest

from app.collector import SourceCollector
from app.dtek.client import FetchError, RawData
from app.notify.router import Router
from app.storage.memory import MemoryStore
from tests.conftest import fixture, kyiv

HOURS_ON = {str(h): "yes" for h in range(1, 25)}
DAY_OCT1, DAY_OCT2 = "1790802000", "1790888400"


def fact(update, days):
    return {"update": update, "today": int(DAY_OCT1), "data": days}


def hours(*off):
    result = dict(HOURS_ON)
    for h in off:
        result[str(h + 1)] = "no"
    return result


class FakeSite:
    def __init__(self):
        self.answer = fixture("kem_gethomenum.json")
        self.fact = fact("01.10.2026 06:50", {DAY_OCT1: {"GPV4.1": hours()}})
        self.fail = False

    async def fetch(self):
        if self.fail:
            raise FetchError("TimeoutError: page did not load")
        return RawData(copy.deepcopy(self.answer), copy.deepcopy(self.fact), {"GPV4.1": "Черга 4.1"}, ["yes", "no"])


@pytest.fixture
def pipeline(settings):
    store, site, clock = MemoryStore(), FakeSite(), {"now": kyiv(2026, 10, 1, 18)}
    collector = SourceCollector(settings.source("kem"), site, store, Router(settings), clock=lambda: clock["now"])
    return collector, site, store, clock


def texts(store):
    return [m.text for m in store.queued() if m.chat_id == "111"]


async def test_first_cycle_initialises_without_messages(pipeline):
    collector, _, store, _ = pipeline
    assert await collector.cycle()
    assert store.queued() == []
    assert store.data.legacy[1]["today"]["date"] == "01.10.26"
    assert collector.healthy()


async def test_tomorrow_published_reaches_every_chat_once(pipeline):
    collector, site, store, clock = pipeline
    await collector.cycle()
    site.fact = fact("01.10.2026 19:40", {DAY_OCT1: {"GPV4.1": hours()}, DAY_OCT2: {"GPV4.1": hours(8, 9)}})
    clock["now"] = kyiv(2026, 10, 1, 19, 41)
    await collector.cycle()
    await collector.cycle()
    assert [m.chat_id for m in store.queued()] == ["111", "222"]
    assert texts(store) == ["📅 <b>Tomorrow's schedule published</b> (02.10)\n\n🔴 08:00 – 10:00"]


async def test_fetch_failures_do_not_touch_the_baseline(pipeline):
    collector, site, store, clock = pipeline
    await collector.cycle()
    baseline_before = json.dumps(store.data.sources["kem"].baseline, sort_keys=True)
    site.fail = True
    for minute in range(1, 6):
        clock["now"] = kyiv(2026, 10, 1, 18, minute)
        assert not await collector.cycle()
    assert collector.failures == 5
    assert json.dumps(store.data.sources["kem"].baseline, sort_keys=True) == baseline_before
    assert store.queued() == []


async def test_failed_commit_loses_nothing(pipeline):
    collector, site, store, clock = pipeline
    await collector.cycle()
    site.fact = fact("01.10.2026 19:40", {DAY_OCT1: {"GPV4.1": hours()}, DAY_OCT2: {"GPV4.1": hours(8)}})
    store.fail_next_commit = True
    assert not await collector.cycle()
    assert store.queued() == []
    # The next successful cycle still sees the change, because the baseline was not committed.
    assert await collector.cycle()
    assert len(texts(store)) == 1


async def test_restart_does_not_resend_or_swallow(pipeline, settings):
    collector, site, store, clock = pipeline
    await collector.cycle()
    # Tomorrow gets published while the service is down...
    site.fact = fact("01.10.2026 19:40", {DAY_OCT1: {"GPV4.1": hours()}, DAY_OCT2: {"GPV4.1": hours(8)}})
    restarted = SourceCollector(settings.source("kem"), site, store, Router(settings), clock=lambda: clock["now"])
    await restarted.restore()
    assert restarted.latest is not None
    # ...and is reported by the first cycle after the restart, exactly once.
    await restarted.cycle()
    await restarted.cycle()
    assert len(texts(store)) == 1


async def test_emergency_notice_and_restoration_change(pipeline):
    collector, site, store, clock = pipeline
    await collector.cycle()
    entry = fixture("outage_entries_20261001.json")["emergency"]
    site.answer["data"]["43"] = dict(entry, sub_type_reason=["GPV4.1"])
    await collector.cycle()
    site.answer["data"]["43"]["end_date"] = "17:30 01.10.2026"
    await collector.cycle()
    sent = texts(store)
    assert sent[0].startswith("🚨 <b>Emergency outage</b>")
    assert sent[1].startswith("🕐 <b>Restoration time changed</b>\n\n15:04 → <b>until 17:30</b>")


async def test_krem_does_not_get_outage_notices(settings):
    store, site = MemoryStore(), FakeSite()
    site.answer = fixture("krem_gethomenum.json")
    site.fact = fact("01.10.2026 06:36", {DAY_OCT1: {"GPV3.1": hours(), "GPV2.2": hours()}})
    clock = lambda: kyiv(2026, 10, 1, 12)
    collector = SourceCollector(settings.source("krem"), site, store, Router(settings), clock=clock)
    await collector.cycle()
    site.answer["data"]["10"] = dict(fixture("outage_entries_20261001.json")["emergency"],
                                     sub_type_reason=["GPV3.1", "GPV2.2"])
    await collector.cycle()
    assert store.queued() == []
    assert collector.latest.outage is not None
