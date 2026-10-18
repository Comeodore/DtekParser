"""deye-reserve's planner on real days: Deye history 09-17.10 and Open-Meteo day-ahead forecasts (previous runs)."""
import bisect
from datetime import date, datetime

from dataclasses import replace

import pytest

from deye_reserve.inputs import combine_forecast, load_profile
from deye_reserve.planner import (Inputs, Params, Planner, Slot, local, pad, profile_table, safe_table, simulate,
                                  table_now, window)
from deye_reserve.timeutil import KYIV_TZ
from tests.conftest import fixture

P = Params()
HISTORY = fixture("deye_history_power_20261009_17.json")["stationDataItems"]


def T(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=KYIV_TZ)


def day_ahead(name="openmeteo_previous_day1_best_match_20261013_18.json"):
    """What the forecast said one day earlier; previous runs have no ensemble, so pessimistic = half the median."""
    return combine_forecast(fixture(name), None, 5.35, T(13, 0))


def plan_for(now, soc, outages, profile_days, forecast=None, params=P, **kw):
    load = load_profile(HISTORY, date(2026, 10, profile_days[0]), date(2026, 10, profile_days[1]), now)
    forecast = forecast or day_ahead()
    inputs = Inputs(now, soc, tuple(outages), load.mean, load.p80, forecast.p50, forecast.pess, **kw)
    return Planner(params).plan(inputs)


def actual(day):
    """The house's real load and PV around `day`, every 5 minutes."""
    points = sorted((datetime.fromtimestamp(i["timeStamp"], KYIV_TZ), i) for i in HISTORY)
    times = [t for t, _ in points]

    def field(name):
        def at(t):
            i = max(0, bisect.bisect_right(times, t) - 1)
            return points[i][1].get(name) or 0
        return at
    return field("consumptionPower"), field("generationPower")


def lowest_in_outages(plan):
    return min(plan.pessimistic.min_by.get(k, 100) for k, (a, _) in enumerate(plan.outages) if a >= plan.start)


def day_charges(table):
    """Grid-charge slots outside the night: (start, end, soc)."""
    out = []
    for i, s in enumerate(table):
        end = table[i + 1].start if i + 1 < len(table) else 1440
        if s.grid_charge and not (s.start >= 23 * 60 or end <= 7 * 60):
            out.append((s.time, f"{end // 60:02d}:{end % 60:02d}", s.soc))
    return out


def test_cloudy_16_10_charges_the_night_high_and_would_have_held():
    # Planned at 23:00 on 15.10 with the first 16.10 schedule (and the night outage known from 15.10).
    outages = [(T(15, 23), T(16, 4)), (T(16, 9, 30), T(16, 11, 30)), (T(16, 18), T(16, 22))]
    plan = plan_for(T(15, 23), 66, outages, (9, 15))
    assert 80 <= plan.night <= 90 and plan.safe
    assert plan.table[0] == Slot(0, plan.night, True) and plan.table[-1] == Slot(23 * 60, plan.night, True)
    assert lowest_in_outages(plan) >= P.outage_min_soc
    # Against what really happened that day (outages as they came: 09:05-15:00, 18:23-22:27) it holds too.
    real = [(T(15, 23), T(16, 4)), (T(16, 9, 5), T(16, 15)), (T(16, 18, 23), T(16, 22, 27))]
    load, pv = actual(16)
    sim = simulate(plan.table, T(15, 23), T(16, 23), 66, load, pv, real, P)
    assert sim.unserved == 0 and min(sim.min_by.values()) >= 35


def test_cloudy_16_10_on_the_services_own_forecast():
    # ECMWF + the median of seven models saw a little more sun (2.4 kWh against best_match's 1.8): a bit lower, still safe.
    outages = [(T(15, 23), T(16, 4)), (T(16, 9, 30), T(16, 11, 30)), (T(16, 18), T(16, 22))]
    models = day_ahead("openmeteo_previous_day1_models_20261013_17.json")
    assert models.total_kwh(date(2026, 10, 16)) == pytest.approx(2.4, abs=0.1)
    plan = plan_for(T(15, 23), 66, outages, (9, 15), forecast=models)
    assert 70 <= plan.night <= 85 and plan.safe and lowest_in_outages(plan) >= P.outage_min_soc


def test_sunny_14_10_leaves_room_for_the_sun():
    plan = plan_for(T(13, 23), 75, [], (9, 13))
    assert plan.night <= 50 and plan.safe
    assert plan.table == safe_table(P, night=plan.night)       # just a lower night target
    # Charging to 90% at night would only have thrown sun away.
    assert plan.expected.cost_adj < plan.baseline.expected.cost_adj - 5


def test_two_outages_in_a_row_are_carried_by_the_night_charge():
    # 16.10's final schedule, cloudy: priced partly by the pessimistic case, a high night target carries both
    # outages, cheaper than topping up at the day tariff before the evening one.
    plan = plan_for(T(15, 23), 66, [(T(16, 9), T(16, 15)), (T(16, 18), T(17, 0))], (9, 15))
    assert plan.safe and lowest_in_outages(plan) >= P.outage_min_soc
    assert plan.night >= 85 and day_charges(plan.table) == []


def test_a_charge_for_tomorrow_is_in_tomorrows_table_only():
    # The same night with the night target capped at 60%: the evening outage needs a top-up on 16.10, and
    # only 16.10's table has it.
    plan = plan_for(T(15, 23), 66, [(T(16, 9), T(16, 15)), (T(16, 18), T(17, 0))], (9, 15),
                    params=replace(P, night_max=60))
    assert plan.safe and lowest_in_outages(plan) >= P.outage_min_soc
    today, tomorrow = plan.tables[date(2026, 10, 15)], plan.tables[date(2026, 10, 16)]
    assert day_charges(today) == []
    (start, end, soc), = day_charges(tomorrow)
    assert end == "18:00" and "15:00" <= start < "18:00" and soc > P.day_floor
    assert all(len(t) == 6 and t[0].start == 0 for t in plan.tables.values())
    # Past 18:00 on the 15th the two tables do the same, so tomorrow's is the one to hold at 23:00.
    assert plan.switch_at == T(15, 18) and plan.table == tomorrow


def test_tomorrows_table_waits_until_it_changes_nothing_today():
    # Planned at 10:00 for a 17:00 outage tomorrow: a charge at 15:30 written now would also run today.
    plan = plan_for(T(16, 10), 60, [(T(17, 17), T(18, 0))], (9, 15), params=replace(P, night_max=60))
    tomorrow = plan.tables[date(2026, 10, 17)]
    (start, end, soc), = day_charges(tomorrow)
    assert end == "17:00" and plan.table == plan.tables[date(2026, 10, 16)] and day_charges(plan.table) == []
    assert plan.switch_at == T(16, 17)
    assert table_now(plan.tables, T(16, 16, 59))[0] != tomorrow and table_now(plan.tables, T(16, 17))[0] == tomorrow


def test_the_simulation_takes_each_days_table():
    today, tomorrow = safe_table(P, night=60), safe_table(P, night=60)[:3] + (Slot(900, 90, True),) + safe_table(P)[4:]
    load = lambda t: 300                                                   # noqa: E731
    one = simulate({date(2026, 10, 16): today, date(2026, 10, 17): tomorrow}, T(16, 14), T(17, 18), 60, load,
                   lambda t: 0, [], P)
    assert one.soc_at[T(16, 22)] == pytest.approx(45, abs=0.5)            # no charge on the 16th
    assert one.soc_end > 85                                                # the 17th's 15:00 charge


def test_outage_from_23_00_is_charged_for_before_the_night_tariff():
    # Planned at 20:00: the night charge would start only once the grid is gone.
    plan = plan_for(T(15, 20), 50, [(T(15, 23), T(16, 4))], (9, 15))
    assert plan.safe and lowest_in_outages(plan) >= P.outage_min_soc
    assert [end for _, end, _ in day_charges(plan.table)] == ["23:00"]


def test_night_outage_after_midnight_is_covered_by_the_night_target():
    plan = plan_for(T(16, 23), 50, [(T(17, 1), T(17, 5))], (10, 16))
    assert plan.safe and lowest_in_outages(plan) >= P.outage_min_soc
    assert day_charges(plan.table) == []
    assert plan.night > P.night_min


def test_calibration_night_goes_to_100():
    plan = plan_for(T(13, 23), 75, [], (9, 13), calibrate=True)
    assert plan.night == 100 and plan.calibration and plan.table[0] == Slot(0, 100, True)


def test_an_outage_under_way_does_not_make_the_rest_unsafe():
    # 10:00 inside a 09:00-15:00 outage with 30% left: it ends near the shutdown SOC and no table changes that,
    # but the evening outage still gets its charge, sized for a battery that comes back nearly empty.
    plan = plan_for(T(16, 10), 30, [(T(16, 9), T(16, 15)), (T(16, 18), T(17, 0))], (9, 15))
    assert plan.pessimistic.min_by[0] < P.outage_min_soc
    assert plan.safe and plan.pessimistic.min_by[1] >= P.outage_min_soc
    (start, end, soc), = day_charges(plan.table)
    assert end == "18:00" and soc >= 55


def test_previous_table_stays_unless_clearly_beaten():
    first = plan_for(T(13, 23), 75, [], (9, 13))
    load = load_profile(HISTORY, date(2026, 10, 9), date(2026, 10, 13), T(13, 23))
    fc = day_ahead()
    nearly = safe_table(P, night=first.night + 5)
    again = Planner(P).plan(Inputs(T(13, 23, 30), 74, (), load.mean, load.p80, fc.p50, fc.pess), previous=nearly)
    assert again.table == nearly and again.kept_previous           # 5% more costs less than the margin
    old = Planner(P).plan(Inputs(T(13, 23, 30), 74, (), load.mean, load.p80, fc.p50, fc.pess), previous=safe_table(P))
    assert old.night <= first.night and not old.kept_previous     # 90% on a sunny day: clearly beaten


def test_window_runs_to_23_00_of_the_coming_nights_day():
    assert [local(t) for t in window(T(17, 15, 3), P)] == [T(17, 15, 0), T(18, 23)]
    assert [local(t) for t in window(T(17, 2, 0), P)] == [T(17, 2, 0), T(17, 23)]
    assert [local(t) for t in window(T(17, 23, 30), P)] == [T(17, 23, 30), T(18, 23)]


def test_six_slots_from_midnight():
    assert pad(profile_table(80, [], P)) == (Slot(0, 80, True), Slot(420, 45, False), Slot(660, 45, False),
                                             Slot(900, 45, False), Slot(1140, 45, False), Slot(1380, 80, True))
    assert safe_table(P) == pad(profile_table(90, [], P))


def test_simulation_keeps_the_floor_while_the_grid_is_there():
    table = safe_table(P, night=60)
    sim = simulate(table, T(16, 23), T(17, 7), 50, lambda t: 500, lambda t: 0, [], P)
    assert sim.soc_end == pytest.approx(60, abs=0.5)                    # charged to the night target, not past it
    day = simulate(table, T(17, 7), T(17, 12), 60, lambda t: 500, lambda t: 0, [], P)
    assert day.soc_end == pytest.approx(45, abs=0.5) and day.buy_day > 0  # down to the day floor, then the grid
    out = simulate(table, T(17, 7), T(17, 12), 60, lambda t: 500, lambda t: 0, [(T(17, 7), T(17, 12))], P)
    assert out.soc_end < 45 and out.buy_day == 0                       # an outage goes below it


def test_forecast_combines_ecmwf_with_the_models_median():
    multi, ens = fixture("openmeteo_models_20261017_1511.json"), fixture("openmeteo_ensemble_20261017_1511.json")
    fc = combine_forecast(multi, ens, 5.35, T(17, 15))
    assert fc.ensemble and fc.covers([date(2026, 10, 17), date(2026, 10, 18)])
    h = multi["hourly"]
    i = h["time"].index("2026-10-18T13:00")                           # the hour 12:00-13:00
    models = [k for k in h if k != "time"]
    vals = sorted(h[k][i] for k in models)
    med = (vals[3] if len(vals) == 7 else None)
    assert fc.p50[(date(2026, 10, 18), 12)] == pytest.approx((med + h["shortwave_radiation_ecmwf_ifs025"][i]) / 2 * 5.35)
    members = sorted(v[i] for k, v in ens["hourly"].items() if k != "time")
    assert fc.pess[(date(2026, 10, 18), 12)] == pytest.approx(min(members[5], med) * 5.35)
    # UKMO has gaps on the 19th: that day goes by the other six models.
    j = h["time"].index("2026-10-19T13:00")
    assert h["shortwave_radiation_ukmo_seamless"][j] is None
    six = sorted(h[k][j] for k in models if "ukmo" not in k)
    assert fc.p50[(date(2026, 10, 19), 12)] == pytest.approx(((six[2] + six[3]) / 2 + h["shortwave_radiation_ecmwf_ifs025"][j]) / 2 * 5.35)
    assert (date(2026, 10, 16), 23) not in fc.p50                      # only the 00:00 stamp of the 17th: not a day


def test_load_profile_from_deye_history():
    load = load_profile(HISTORY, date(2026, 10, 9), date(2026, 10, 15), T(16, 0))
    assert load.days == 7 and set(load.mean) == set(range(24))
    assert 7 < sum(load.mean.values()) / 1000 < 10                    # kWh a day
    assert sum(load.p80.values()) > sum(load.mean.values())
    assert load.last_full == date(2026, 10, 14)                       # the sunny 14th filled the battery
