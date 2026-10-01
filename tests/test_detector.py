from datetime import date

from app.engine.detector import ABSENT_CONFIRMATIONS, CLEAR_CONFIRMATIONS, Baseline, detect
from app.engine.events import OutageChange, ScheduleChange
from tests.conftest import day, emergency, kyiv, one, slots, snapshot

FP = "address"
OCT1, OCT2, OCT3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)


def run(baseline, snap, now):
    result = detect(baseline, snap, now, FP)
    # Every baseline must survive a restart.
    assert Baseline.from_dict(result.baseline.to_dict()) == result.baseline
    return result


def changes(result):
    return [(e.day, e.change) for e in result.events if hasattr(e, "day")]


def test_first_run_is_silent():
    result = run(None, snapshot([one(OCT1, [16, 17])]), kyiv(2026, 10, 1, 10))
    assert result.initialized and result.events == []
    assert result.baseline.days[OCT1].has_outages()


def test_address_change_reinitialises_silently():
    base = run(None, snapshot([one(OCT1, [16])]), kyiv(2026, 10, 1, 10)).baseline
    result = detect(base, snapshot([one(OCT1, [9])]), kyiv(2026, 10, 1, 10), "other address")
    assert result.initialized and result.events == []


def test_tomorrow_published_once():
    base = run(None, snapshot([one(OCT1)]), kyiv(2026, 10, 1, 18)).baseline
    result = run(base, snapshot([one(OCT1), one(OCT2, [8, 9])]), kyiv(2026, 10, 1, 20))
    assert changes(result) == [(OCT2, ScheduleChange.PUBLISHED)]
    again = run(result.baseline, snapshot([one(OCT1), one(OCT2, [8, 9])]), kyiv(2026, 10, 1, 20, 1))
    assert again.events == []


def test_midnight_needs_no_special_casing():
    base = run(None, snapshot([one(OCT1, [10]), one(OCT2, [8, 9])]), kyiv(2026, 10, 1, 23, 59)).baseline
    # Same data after midnight: yesterday's tomorrow is today now, nothing changed.
    result = run(base, snapshot([one(OCT1, [10]), one(OCT2, [8, 9])]), kyiv(2026, 10, 2, 0, 1))
    assert result.events == []
    assert set(result.baseline.days) == {OCT2}


def test_todays_incident_schedule_returning_after_a_blank_night_is_reported():
    # 30.09 evening: today known, tomorrow not captured (the parser was stuck).
    base = run(None, snapshot([one(date(2026, 9, 30), [9])]), kyiv(2026, 9, 30, 19, 39)).baseline
    # 01.10 01:35: the schedule for 01.10 shows up for the first time.
    result = run(base, snapshot([one(OCT1, [16, 17, 18])]), kyiv(2026, 10, 1, 1, 35))
    assert changes(result) == [(OCT1, ScheduleChange.PUBLISHED)]


def test_change_in_remaining_hours_is_reported():
    base = run(None, snapshot([one(OCT1, [16, 17, 18])]), kyiv(2026, 10, 1, 6)).baseline
    result = run(base, snapshot([one(OCT1, [17, 18, 19])]), kyiv(2026, 10, 1, 6, 50))
    assert changes(result) == [(OCT1, ScheduleChange.CHANGED)]


def test_change_in_past_hours_is_not_news():
    base = run(None, snapshot([one(OCT1, [8, 16])]), kyiv(2026, 10, 1, 12)).baseline
    result = run(base, snapshot([one(OCT1, [9, 16])]), kyiv(2026, 10, 1, 12, 1))
    assert result.events == []
    assert result.baseline.days[OCT1].lines[0].slots == slots([9, 16])


def test_todays_outages_cancelled():
    # What DTEK did at 06:50 on 01.10.
    base = run(None, snapshot([one(OCT1, [16, 17, 18])]), kyiv(2026, 10, 1, 1, 35)).baseline
    result = run(base, snapshot([one(OCT1)]), kyiv(2026, 10, 1, 6, 51))
    assert changes(result) == [(OCT1, ScheduleChange.CLEARED)]


def test_tomorrow_cancelled():
    base = run(None, snapshot([one(OCT1), one(OCT2, [8])]), kyiv(2026, 10, 1, 20)).baseline
    result = run(base, snapshot([one(OCT1), one(OCT2)]), kyiv(2026, 10, 1, 21))
    assert changes(result) == [(OCT2, ScheduleChange.CLEARED)]


def test_vanished_day_needs_confirmation():
    base = run(None, snapshot([one(OCT1), one(OCT2, [8])]), kyiv(2026, 10, 1, 20)).baseline
    for i in range(ABSENT_CONFIRMATIONS - 1):
        result = run(base, snapshot([one(OCT1)]), kyiv(2026, 10, 1, 20, i + 1))
        assert result.events == []
        assert result.baseline.days[OCT2].has_outages()
        base = result.baseline
    result = run(base, snapshot([one(OCT1)]), kyiv(2026, 10, 1, 20, 10))
    assert changes(result) == [(OCT2, ScheduleChange.CLEARED)]
    assert result.events[0].schedule is None
    assert OCT2 not in result.baseline.days


def test_day_flickering_away_and_back_is_silent():
    base = run(None, snapshot([one(OCT1), one(OCT2, [8])]), kyiv(2026, 10, 1, 20)).baseline
    base = run(base, snapshot([one(OCT1)]), kyiv(2026, 10, 1, 20, 1)).baseline
    result = run(base, snapshot([one(OCT1), one(OCT2, [8])]), kyiv(2026, 10, 1, 20, 2))
    assert result.events == []
    assert result.baseline.absent == {}


def test_change_on_second_line_is_reported():
    base = run(None, snapshot([day(OCT2, ("GPV3.1", slots([8])), ("GPV2.2", slots([12])))]),
               kyiv(2026, 10, 1, 20)).baseline
    result = run(base, snapshot([day(OCT2, ("GPV3.1", slots([8])), ("GPV2.2", slots([13])))]),
                 kyiv(2026, 10, 1, 21))
    assert changes(result) == [(OCT2, ScheduleChange.CHANGED)]


def test_group_reassignment_is_a_change():
    base = run(None, snapshot([one(OCT2, [8], group="GPV4.1")]), kyiv(2026, 10, 1, 20)).baseline
    result = run(base, snapshot([one(OCT2, [10], group="GPV5.1")]), kyiv(2026, 10, 1, 21))
    assert changes(result) == [(OCT2, ScheduleChange.CHANGED)]


def test_schedule_published_during_an_emergency_is_still_reported():
    # LightBot used to drop schedule changes while DTEK showed an emergency.
    base = run(None, snapshot([one(OCT1)], outage=emergency()), kyiv(2026, 10, 1, 19)).baseline
    result = run(base, snapshot([one(OCT1), one(OCT2, [8])], outage=emergency()), kyiv(2026, 10, 1, 20))
    assert changes(result) == [(OCT2, ScheduleChange.PUBLISHED)]


def test_schedule_changes_are_tracked_silently_while_schedules_are_suspended():
    suspended = emergency(suspended=True)
    base = run(None, snapshot([one(OCT1)], outage=suspended), kyiv(2026, 10, 1, 19)).baseline
    result = run(base, snapshot([one(OCT1), one(OCT2, [8])], outage=suspended), kyiv(2026, 10, 1, 20))
    assert changes(result) == []
    assert result.baseline.days[OCT2].has_outages()


def test_outage_lifecycle():
    base = run(None, snapshot([one(OCT1)]), kyiv(2026, 10, 1, 11)).baseline

    started = run(base, snapshot([one(OCT1)], outage=emergency()), kyiv(2026, 10, 1, 11, 5))
    assert [e.change for e in started.events] == [OutageChange.STARTED]

    same = run(started.baseline, snapshot([one(OCT1)], outage=emergency()), kyiv(2026, 10, 1, 11, 6))
    assert same.events == []

    moved = run(same.baseline, snapshot([one(OCT1)], outage=emergency(end="17:30 01.10.2026")),
                kyiv(2026, 10, 1, 13))
    assert [e.change for e in moved.events] == [OutageChange.RESTORATION_CHANGED]
    assert moved.events[0].previous.end == "15:04 01.10.2026"

    base = moved.baseline
    for i in range(CLEAR_CONFIRMATIONS - 1):
        pending = run(base, snapshot([one(OCT1)]), kyiv(2026, 10, 1, 17, 31 + i))
        assert pending.events == []
        base = pending.baseline
    ended = run(base, snapshot([one(OCT1)]), kyiv(2026, 10, 1, 17, 40))
    assert [e.change for e in ended.events] == [OutageChange.ENDED]
    assert ended.baseline.outage is None


def test_outage_flicker_is_silent():
    base = run(None, snapshot([one(OCT1)], outage=emergency()), kyiv(2026, 10, 1, 11)).baseline
    base = run(base, snapshot([one(OCT1)]), kyiv(2026, 10, 1, 11, 1)).baseline
    result = run(base, snapshot([one(OCT1)], outage=emergency()), kyiv(2026, 10, 1, 11, 2))
    assert result.events == []
    assert result.baseline.clear_streak == 0


def test_new_outage_replacing_the_old_one():
    base = run(None, snapshot([one(OCT1)], outage=emergency()), kyiv(2026, 10, 1, 11)).baseline
    result = run(base, snapshot([one(OCT1)], outage=emergency(start="16:00 01.10.2026")), kyiv(2026, 10, 1, 16))
    assert [e.change for e in result.events] == [OutageChange.STARTED]


def test_outage_reason_change():
    base = run(None, snapshot([one(OCT1)], outage=emergency()), kyiv(2026, 10, 1, 11)).baseline
    result = run(base, snapshot([one(OCT1)], outage=emergency(suspended=True)), kyiv(2026, 10, 1, 11, 1))
    assert [e.change for e in result.events] == [OutageChange.UPDATED]


def test_days_outside_today_and_tomorrow_are_ignored():
    base = run(None, snapshot([one(OCT1)]), kyiv(2026, 10, 1, 12)).baseline
    result = run(base, snapshot([one(OCT1), one(OCT3, [5])]), kyiv(2026, 10, 1, 12, 1))
    assert result.events == []
    assert set(result.baseline.days) == {OCT1}
