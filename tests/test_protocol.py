from datetime import date

import pytest

from app.dtek.models import Availability, OutageKind, Slot
from app.dtek.protocol import ProtocolError, build_snapshot, parse_fact, parse_house, unknown_time_types
from tests.conftest import fixture, kyiv


def test_page_fact_matches_what_the_site_showed():
    page = fixture("kem_page_20261001_0116.json")
    fact = parse_fact(page["fact"])
    assert fact.update == "30.09.2026 23:18"
    assert list(fact.days) == [date(2026, 10, 1)]
    gpv41 = fact.days[date(2026, 10, 1)]["GPV4.1"]
    # dtek-kem.com.ua at 00:58: no power 16-17, 17-18, 18-19
    assert [h for h, s in enumerate(gpv41) if s.is_outage] == [16, 17, 18]
    assert gpv41[16] is Slot.OFF
    assert fact.days[date(2026, 10, 1)]["GPV5.1"][18] is Slot.SECOND_HALF_OFF


def test_house_with_one_group():
    house = parse_house(fixture("kem_gethomenum.json"), "43")
    assert house.groups == ("GPV4.1",)
    assert house.outage is None
    assert house.availability is Availability.OK
    assert house.outage_updated == "12:33 01.10.2026"


def test_house_fed_by_two_lines():
    house = parse_house(fixture("krem_gethomenum.json"), "10")
    assert house.groups == ("GPV3.1", "GPV2.2")


@pytest.mark.parametrize("sample, kind", [("planned", OutageKind.PLANNED), ("emergency", OutageKind.EMERGENCY)])
def test_outage_notice(sample, kind):
    answer = {"result": True, "updateTimestamp": "12:50 01.10.2026",
              "data": {"5": fixture("outage_entries_20261001.json")[sample]}}
    outage = parse_house(answer, "5").outage
    assert outage.kind is kind
    assert outage.end_at == kyiv(2026, 10, 1, 15, 0 if sample == "planned" else 4)
    assert not outage.schedules_suspended


def test_national_emergency_suspends_schedules():
    entry = dict(fixture("outage_entries_20261001.json")["emergency"],
                 sub_type="Екстренні відключення (Аварійне без застосування графіку погодинних відключень)")
    outage = parse_house({"result": True, "data": {"5": entry}}, "5").outage
    assert outage.kind is OutageKind.EMERGENCY
    assert outage.schedules_suspended


def test_snapshot_for_two_lines():
    house = parse_house(fixture("krem_gethomenum.json"), "10")
    fact = parse_fact(fixture("krem_fact_20261001_0636.json"))
    snap = build_snapshot("krem", house, fact, {"GPV3.1": "Черга 3.1", "GPV2.2": "Черга 2.2"}, kyiv(2026, 10, 1, 13))
    today = snap.day(date(2026, 10, 1))
    assert [line.group for line in today.lines] == ["GPV3.1", "GPV2.2"]
    assert [line.name for line in today.lines] == ["Черга 3.1", "Черга 2.2"]


def test_building_managed_house_has_no_schedule():
    entry = dict(fixture("kem_gethomenum.json")["data"]["43"], voluntarily=True)
    house = parse_house({"result": True, "data": {"43": entry}}, "43")
    page = fixture("kem_page_20261001_0116.json")
    snap = build_snapshot("kem", house, parse_fact(page["fact"]), page["names"], kyiv(2026, 10, 1, 1))
    assert house.availability is Availability.BUILDING_MANAGED
    assert snap.days == ()


def test_empty_fact_data_is_php_empty_array():
    assert parse_fact({"update": "x", "data": []}).days == {}


@pytest.mark.parametrize("answer, building, message", [
    ({"result": False}, "43", "result=False"),
    ({"result": True, "data": []}, "43", "data is list"),
    ({"result": True, "data": {"41": {}}}, "43", "'43' is not in the answer"),
])
def test_bad_answers_fail_loudly(answer, building, message):
    with pytest.raises(ProtocolError, match=message):
        parse_house(answer, building)


def test_unknown_slot_value_fails_loudly():
    hours = {str(h): "yes" for h in range(1, 25)}
    hours["5"] = "probably"
    with pytest.raises(ProtocolError, match="unknown slot value 'probably'"):
        parse_fact({"update": "x", "data": {"1790802000": {"GPV4.1": hours}}})


def test_missing_hours_fail_loudly():
    hours = {str(h): "yes" for h in range(1, 24)}
    with pytest.raises(ProtocolError, match="unexpected hour keys"):
        parse_fact({"update": "x", "data": {"1790802000": {"GPV4.1": hours}}})


def test_day_key_must_be_kyiv_midnight():
    hours = {str(h): "yes" for h in range(1, 25)}
    with pytest.raises(ProtocolError, match="not a Kyiv midnight"):
        parse_fact({"update": "x", "data": {"1790805600": {"GPV4.1": hours}}})


def test_unknown_time_types():
    assert unknown_time_types(["yes", "no", "maybe", "later"]) == ["later"]
