"""The single-instance lock: how PostgresStore takes it, without a database."""
from contextlib import asynccontextmanager

import pytest

from app.storage import postgres
from app.storage.postgres import PostgresStore


class FakeConn:
    def __init__(self, lock_answers):
        self.lock_answers = list(lock_answers)
        self.closed = False

    async def execute(self, *args):
        return "OK"

    async def fetchval(self, query, *args):
        assert "pg_try_advisory_lock" in query
        return self.lock_answers.pop(0)

    async def fetchrow(self, query, *args):
        return None

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


class FakePool:
    @asynccontextmanager
    async def acquire(self):
        yield FakeConn([])


@pytest.fixture
def db(monkeypatch):
    calls = {"connect": []}

    def setup(lock_answers):
        lock_conn = FakeConn(lock_answers)

        async def connect(url, timeout=None, server_settings=None):
            calls["connect"].append(server_settings)
            # The first connection only bootstraps the schema; the second holds the lock.
            return FakeConn([]) if len(calls["connect"]) == 1 else lock_conn

        async def create_pool(url, **kwargs):
            calls["pool"] = kwargs["server_settings"]
            return FakePool()

        monkeypatch.setattr(postgres.asyncpg, "connect", connect)
        monkeypatch.setattr(postgres.asyncpg, "create_pool", create_pool)
        return lock_conn

    return setup, calls


async def test_the_lock_and_pool_sessions_ask_the_server_to_probe_the_client(db):
    setup, calls = db
    setup([True])
    store = PostgresStore("postgresql://x/y")
    await store.connect()
    for settings in (calls["connect"][1], calls["pool"]):
        assert settings["tcp_keepalives_idle"] == "60"
        assert settings["tcp_keepalives_interval"] == "10"
        assert settings["tcp_keepalives_count"] == "3"
        assert settings["tcp_user_timeout"] == "90000"
        assert settings["application_name"] == "DtekService"


async def test_a_starting_instance_waits_for_the_lock_to_be_released(db):
    setup, _ = db
    lock_conn = setup([False, False, True])
    store = PostgresStore("postgresql://x/y")
    store.LOCK_POLL_S = 0
    await store.connect()
    assert lock_conn.lock_answers == [] and not lock_conn.closed


async def test_it_gives_up_when_the_lock_stays_held(db):
    setup, _ = db
    lock_conn = setup([False] * 100)
    store = PostgresStore("postgresql://x/y")
    store.LOCK_WAIT_S = 0
    store.LOCK_POLL_S = 0
    with pytest.raises(RuntimeError, match="already holds the lock"):
        await store.connect()
    assert lock_conn.closed
    assert store._lock_conn is None
