"""The whole pipeline (site -> detector -> store -> outbox) with fakes at the edges."""
import copy
import json
from datetime import timedelta

import pytest

from app.collector import SourceCollector, error_kind
from app.dtek.client import FetchError, RawData, SiteDown
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


async def test_first_error_names_a_run_of_failures(pipeline):
    collector, site, _, clock = pipeline
    site.fail = True
    assert not await collector.cycle()
    original = site.fetch

    async def http_502():
        raise FetchError("Error: getHomeNum HTTP 502: <html>")
    site.fetch = http_502
    assert not await collector.cycle()
    # The alert's reason stays the first error's; the log shows the latest.
    assert collector.first_error == "fetch: TimeoutError: page did not load"
    assert collector.last_error == "fetch: Error: getHomeNum HTTP 502: <html>"
    site.fetch, site.fail = original, False
    assert await collector.cycle()
    assert (collector.first_error, collector.last_error) == ("", "")


@pytest.mark.parametrize("error, kind", [
    ("fetch: Error: Page.evaluate: Error: getHomeNum HTTP 502: <html>", "HTTP 502"),
    ("fetch: TimeoutError: Page.wait_for_function: Timeout 29999ms exceeded.", "timeout"),
    ("fetch: Incapsula shows an hCaptcha; solve it with tools/solve_captcha.sh", "Incapsula challenge"),
    ("fetch: getHomeNum answered result=False", "fetch: getHomeNum"),
    ("store: ConnectionRefusedError: [Errno 111]", "store: ConnectionRefusedError"),
])
def test_error_kind(error, kind):
    assert error_kind(error) == kind


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


class SiteDownSite(FakeSite):
    """FakeSite whose failures are chosen per cycle: SiteDown (DTEK's trouble) or FetchError (ours)."""

    def __init__(self):
        super().__init__()
        self.error = None

    async def fetch(self):
        if self.error is not None:
            raise self.error
        return await super().fetch()


@pytest.fixture
def down_pipeline(settings):
    store, site, clock = MemoryStore(), SiteDownSite(), {"now": kyiv(2026, 10, 1, 18)}
    collector = SourceCollector(settings.source("kem"), site, store, Router(settings), clock=lambda: clock["now"])
    return collector, site, clock


async def fail_until(collector, clock, minutes):
    for minute in minutes:
        clock["now"] = kyiv(2026, 10, 1, 18) + timedelta(minutes=minute)
        assert not await collector.cycle()


async def test_dtek_site_down_is_waited_out_without_alarm(down_pipeline):
    collector, site, clock = down_pipeline
    assert await collector.cycle()
    site.error = SiteDown("TimeoutError: Page.goto: Timeout 45000ms exceeded.")
    await fail_until(collector, clock, [1, 2, 3, 60, 24 * 60])
    assert not collector.healthy() and collector.problem() is None
    site.error = None
    assert await collector.cycle()
    assert collector.problem() is None and not collector.site_down and collector.ours_since is None


async def test_our_own_failure_alarms_within_minutes_as_before(down_pipeline):
    collector, site, clock = down_pipeline
    assert await collector.cycle()
    site.error = FetchError("Incapsula shows an hCaptcha; solve it with tools/solve_captcha.sh kem")
    await fail_until(collector, clock, [1, 2])
    assert collector.problem() is None
    await fail_until(collector, clock, [3])
    assert collector.problem() == "no DTEK data since 01.10 18:00 (Incapsula challenge)"


async def test_our_failure_after_the_site_was_down_counts_from_its_own_start(down_pipeline):
    collector, site, clock = down_pipeline
    assert await collector.cycle()
    site.error = SiteDown("Error: Page.evaluate: Error: getHomeNum timed out after 15000 ms")
    await fail_until(collector, clock, range(1, 61))
    assert collector.problem() is None
    site.error = FetchError("page has no schedule data (status 200, complete, 'DTEK': 'new layout')")
    await fail_until(collector, clock, [61, 62, 63])
    assert collector.problem() is None
    await fail_until(collector, clock, [64])
    assert collector.problem() == "no DTEK data since 01.10 18:00 (page not recognised)"
    # The site going down again puts the alarm back to waiting.
    site.error = SiteDown("TimeoutError: Page.goto: Timeout 45000ms exceeded.")
    await fail_until(collector, clock, [65])
    assert collector.problem() is None


@pytest.mark.parametrize("error, kind", [
    ("fetch: Error: Page.evaluate: AbortError: signal is aborted without reason", "timeout"),
    ("fetch: Error: Page.evaluate: Error: getHomeNum timed out after 15000 ms", "timeout"),
    ("fetch: SiteDown: DTEK's site shows it is down or overloaded (status 200, complete, '': 'x')", "site overloaded"),
    ("fetch: FetchError: page has no schedule data (status 200, complete, 'DTEK': 'x')", "page not recognised"),
    ("fetch: SiteDown: page still loading after 30 s (status 200, interactive, '': '')", "timeout"),
])
def test_error_kind_of_dtek_trouble(error, kind):
    assert error_kind(error) == kind
