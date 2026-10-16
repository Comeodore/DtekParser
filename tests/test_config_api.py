import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api import create_app, scheduled_status
from app.config import ConfigError, load_settings
from app.service import Service
from app.storage.memory import MemoryStore
from app.wol import magic_packet
from tests.conftest import BASE_ENV, day, emergency, kyiv, one, slots, snapshot


def test_settings(settings):
    kem, krem = settings.sources
    assert (kem.port, kem.lang, kem.bot, kem.chat_ids, kem.notify_outages) == (9999, "en", "lightbot", ("111", "222"), True)
    assert (krem.settlement, krem.schedule_id, krem.notify_outages) == ("с. Круглик", 2, False)
    assert settings.power.source == "kem"
    assert kem.fingerprint != krem.fingerprint


@pytest.mark.parametrize("change, message", [
    ({"SOURCES": ""}, "SOURCES is required"),
    ({"KEM_BOT": "nobot"}, "BOT_NOBOT_TOKEN is not set"),
    ({"KREM_PORT": "9999"}, "its own"),
    ({"KEM_LANG": "de"}, "KEM_LANG"),
    ({"KREM_NOTIFY_OUTAGES": "maybe"}, "not a boolean"),
])
def test_bad_settings(change, message):
    with pytest.raises(ConfigError, match=message):
        load_settings({**BASE_ENV, **change})


def test_magic_packet():
    packet = magic_packet("BC:FC:E7:1A:15:90")
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6 and packet[6:12] == bytes.fromhex("BCFCE71A1590")
    with pytest.raises(ValueError):
        magic_packet("not-a-mac")


@pytest.fixture
def client(settings):
    service = Service(settings, store=MemoryStore(), gateway=object())
    now = kyiv(2026, 10, 1, 12)
    service.collectors["kem"].latest = snapshot([one(now.date(), [16, 17, 18])], outage=emergency())
    service.collectors["kem"].last_success = now
    # No `with`: the lifespan (browser, loops) is not started in tests.
    return TestClient(create_app(service)), service


def test_schedule_for_explicit_source(client, monkeypatch):
    http, _ = client
    monkeypatch.setattr("app.api.now_kyiv", lambda: kyiv(2026, 10, 1, 12, 1))
    body = http.get("/api/v1/schedule?source=kem").json()
    assert body["lang"] == "en" and body["healthy"]
    assert body["outage"]["kind"] == "emergency"
    assert body["scheduled_status"] == {"state": "on", "start": "", "end": ""}
    assert body["current_outage"]["status"] == "emergency"
    assert [s["hour"] for s in body["today"]["slots"] if s["status"] != "power_on"] == [16, 17, 18]
    assert body["days"][0]["lines"][0]["name"] == "Черга 4.1"
    assert "address" not in body


@pytest.mark.parametrize("at, expected", [
    (kyiv(2026, 10, 1, 9), {"state": "on", "start": "", "end": ""}),
    (kyiv(2026, 10, 1, 12, 15), {"state": "maybe", "start": "12:00", "end": "14:00"}),
    (kyiv(2026, 10, 1, 13, 40), {"state": "off", "start": "12:00 / 13:30", "end": "14:00 / 15:00"}),
    (kyiv(2026, 10, 1, 23, 10), {"state": "off", "start": "22:00", "end": "завтра 02:00"}),
])
def test_scheduled_status(at, expected):
    today = day(date(2026, 10, 1), ("GPV3.1", slots(off=[12, 13, 22, 23])), ("GPV2.2", slots(off=[14, 22, 23], second=[13])))
    tomorrow = day(date(2026, 10, 2), ("GPV3.1", slots(off=[0, 1])), ("GPV2.2", slots(off=[0, 1])))
    assert scheduled_status(snapshot([today, tomorrow], source="krem"), at, "uk") == expected


def test_no_scheduled_status_without_a_schedule_in_force():
    now = kyiv(2026, 10, 1, 12)
    assert scheduled_status(snapshot([one(date(2026, 10, 2), [12])]), now, "en") is None
    suspended = snapshot([one(now.date(), [12])], outage=emergency(suspended=True))
    assert scheduled_status(suspended, now, "en") is None


def test_source_without_data_yet(client):
    http, _ = client
    body = http.get("/api/v1/schedule?source=krem").json()
    assert body["today"] is None and body["days"] == [] and not body["healthy"]
    assert http.get("/api/v1/schedule?source=nope").status_code == 404


def test_health_reports_degraded_source(client, monkeypatch):
    http, _ = client
    monkeypatch.setattr("app.api.now_kyiv", lambda: kyiv(2026, 10, 1, 12, 1))
    response = http.get("/api/v1/health")
    assert response.status_code == 503
    assert response.json()["sources"]["kem"]["healthy"] is True
    assert response.json()["sources"]["krem"]["healthy"] is False


def test_wol_endpoint(client, monkeypatch):
    http, _ = client
    sent = []
    monkeypatch.setattr("app.api.send_magic_packet", lambda mac, bcast: sent.append((mac, bcast)))
    assert http.post("/api/v1/wol").json() == {"status": "ok"}
    assert sent == [("BC:FC:E7:1A:15:90", "255.255.255.255")]


def test_mini_app_page(client):
    http, _ = client
    response = http.get("/")
    assert response.status_code == 200 and "DTEK" in response.text


def test_problems_name_each_failing_check(client):
    _, service = client
    now = kyiv(2026, 10, 1, 12, 1)
    service.collectors["krem"].last_error = "fetch: TimeoutError: page did not load"
    problems = service.problems(now)
    assert problems == [
        "krem: no successful DTEK fetch since start: fetch: TimeoutError: page did not load",
        "power: Home Assistant is not connected",
        "outbox: message delivery loop is not cycling",
    ]
    service.collectors["krem"].last_success = now
    service.power.connected = True
    service.outbox.last_cycle = time.monotonic()
    assert service.problems(now) == []
