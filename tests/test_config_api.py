import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import ConfigError, load_settings
from app.service import Service
from app.storage.memory import MemoryStore
from app.wol import magic_packet
from tests.conftest import BASE_ENV, emergency, kyiv, one, snapshot


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
    assert body["current_outage"]["status"] == "emergency"
    assert [s["hour"] for s in body["today"]["slots"] if s["status"] != "power_on"] == [16, 17, 18]
    assert body["days"][0]["lines"][0]["name"] == "Черга 4.1"
    assert "address" not in body


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
