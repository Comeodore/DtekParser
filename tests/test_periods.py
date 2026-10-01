from app.engine.periods import continues_into, current_period, next_period, outage_periods, upcoming_periods
from tests.conftest import slots


def test_adjacent_slots_merge():
    assert outage_periods(slots([16, 17, 18])) == [(960, 1140)]


def test_half_hours():
    s = slots([20, 21], second=[19])
    assert outage_periods(s) == [(1170, 1320)]
    assert outage_periods(slots(first=[9], second=[10])) == [(540, 570), (630, 660)]


def test_current_next_upcoming():
    s = slots([9, 10, 16, 17])
    assert current_period(s, 9 * 60 + 30) == (540, 660)
    assert next_period(s, 9 * 60 + 30) == (960, 1080)
    assert upcoming_periods(s, 9 * 60 + 30) == [(540, 660), (960, 1080)]
    assert upcoming_periods(s, 12 * 60) == [(960, 1080)]
    assert next_period(s, -1) == (540, 660)


def test_cross_midnight():
    assert continues_into(slots([22, 23]), slots([0, 1]), (1320, 1440)) == 120
    assert continues_into(slots([22, 23]), slots([5]), (1320, 1440)) is None
    assert continues_into(slots([22, 23]), None, (1320, 1440)) is None
