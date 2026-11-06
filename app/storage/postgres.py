from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Optional, Sequence

import asyncpg

from app.notify.router import OutgoingMessage
from app.power.state import PowerState, PowerStatus
from app.storage.store import OutboxItem, SourceState
from app.timeutil import KYIV_TZ

logger = logging.getLogger(__name__)

# Postgres learns that a client is gone only from the client's FIN or RST, or else from TCP
# keepalive, which by default first probes after two hours idle. A client that vanished with the
# network (its address dropped, so not even a reset got out) kept its session, and with it the
# instance lock, for those two hours. Asked to probe after a minute idle, the server ends such a
# session about 90 s after it last heard from the client, or at once when a probe meets a reset.
# A live client answers the probes from its kernel. The service gives the lock up sooner on its
# own when it cannot reach the server: it checks the lock every 30 s with a 10 s timeout.
SESSION_SETTINGS = {
    "tcp_keepalives_idle": "60",
    "tcp_keepalives_interval": "10",
    "tcp_keepalives_count": "3",
    # The same bound while a reply is still unacknowledged, when keepalives do not run (ms).
    "tcp_user_timeout": "90000",
}

# dtek_schedule and power_events predate this service and are kept as they were.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dtek_schedule (
    id integer PRIMARY KEY,
    today_schedule jsonb,
    tomorrow_schedule jsonb,
    updated_at text,
    current_outage jsonb
);
CREATE TABLE IF NOT EXISTS power_events (
    id integer PRIMARY KEY,
    state varchar NOT NULL,
    timestamp timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS dtek_source_state (
    source text PRIMARY KEY,
    snapshot jsonb,
    baseline jsonb,
    last_success_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id bigserial PRIMARY KEY,
    bot text NOT NULL,
    chat_id text NOT NULL,
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    -- NULL: due now. Set only from the service clock, never from the database's now(),
    -- so a clock difference between the two cannot delay a fresh message.
    next_attempt_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    sent_at timestamptz,
    failed_at timestamptz,
    last_error text
);
ALTER TABLE notification_outbox ALTER COLUMN next_attempt_at DROP NOT NULL;
ALTER TABLE notification_outbox ALTER COLUMN next_attempt_at DROP DEFAULT;
CREATE INDEX IF NOT EXISTS notification_outbox_pending
    ON notification_outbox (id) WHERE sent_at IS NULL AND failed_at IS NULL;
"""


def _lock_key(schema: str) -> int:
    digest = hashlib.sha256(f"dtek-service:{schema}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class PgTx:
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def load_source_state(self, source: str) -> Optional[SourceState]:
        row = await self._conn.fetchrow(
            "SELECT snapshot, baseline, last_success_at FROM dtek_source_state WHERE source = $1 FOR UPDATE",
            source,
        )
        return _source_state(row)

    async def save_source_state(self, source: str, snapshot: dict, baseline: dict, success_at: datetime) -> None:
        await self._conn.execute(
            """
            INSERT INTO dtek_source_state (source, snapshot, baseline, last_success_at, updated_at)
            VALUES ($1, $2::jsonb, $3::jsonb, $4, now())
            ON CONFLICT (source) DO UPDATE
               SET snapshot = EXCLUDED.snapshot, baseline = EXCLUDED.baseline,
                   last_success_at = EXCLUDED.last_success_at, updated_at = now()
            """,
            source, json.dumps(snapshot, ensure_ascii=False), json.dumps(baseline, ensure_ascii=False), success_at,
        )

    async def save_legacy_schedule(self, schedule_id: int, row: dict) -> None:
        values = (
            json.dumps(row["current_outage"], ensure_ascii=False),
            json.dumps(row["today"], ensure_ascii=False) if row["today"] else None,
            json.dumps(row["tomorrow"], ensure_ascii=False) if row["tomorrow"] else None,
            row["updated_at"],
            schedule_id,
        )
        status = await self._conn.execute(
            """
            UPDATE dtek_schedule
               SET current_outage = $1::jsonb, today_schedule = $2::jsonb,
                   tomorrow_schedule = $3::jsonb, updated_at = $4
             WHERE id = $5
            """,
            *values,
        )
        if status == "UPDATE 0":
            await self._conn.execute(
                """
                INSERT INTO dtek_schedule (current_outage, today_schedule, tomorrow_schedule, updated_at, id)
                VALUES ($1::jsonb, $2::jsonb, $3::jsonb, $4, $5)
                """,
                *values,
            )

    async def load_power_state(self, slot: int = 1) -> Optional[PowerState]:
        row = await self._conn.fetchrow("SELECT state, timestamp FROM power_events WHERE id = $1 FOR UPDATE", slot)
        if row is None:
            return None
        return PowerState(PowerStatus(row["state"]), row["timestamp"])

    async def save_power_state(self, state: PowerState, slot: int = 1) -> None:
        status = await self._conn.execute(
            "UPDATE power_events SET state = $1, timestamp = $2 WHERE id = $3", state.status.value, state.since, slot
        )
        if status == "UPDATE 0":
            await self._conn.execute(
                "INSERT INTO power_events (id, state, timestamp) VALUES ($3, $1, $2)",
                state.status.value, state.since, slot,
            )

    async def enqueue(self, messages: Sequence[OutgoingMessage]) -> None:
        if not messages:
            return
        await self._conn.executemany(
            "INSERT INTO notification_outbox (bot, chat_id, text, expires_at) VALUES ($1, $2, $3, $4)",
            [(m.bot, m.chat_id, m.text, m.expires_at) for m in messages],
        )


def _source_state(row: Optional[asyncpg.Record]) -> Optional[SourceState]:
    if row is None:
        return None
    return SourceState(_json(row["snapshot"]), _json(row["baseline"]), row["last_success_at"])


def _json(value) -> Optional[dict]:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


class PostgresStore:
    # A starting instance waits this long for the lock before it gives up and is restarted, so it
    # takes over within seconds once the server has ended the previous instance's session.
    LOCK_WAIT_S = 120.0
    LOCK_POLL_S = 5.0

    def __init__(self, url: str, schema: str = "public"):
        self._url = url
        self._schema = schema
        self._pool: Optional[asyncpg.Pool] = None
        self._lock_conn: Optional[asyncpg.Connection] = None

    async def connect(self) -> None:
        settings = {"application_name": "DtekService", "search_path": self._schema, **SESSION_SETTINGS}
        bootstrap = await asyncpg.connect(self._url, timeout=15)
        try:
            if self._schema != "public":
                await bootstrap.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
        finally:
            await bootstrap.close()

        # One instance per schema: a second one would send every message twice.
        self._lock_conn = await asyncpg.connect(self._url, timeout=15, server_settings=settings)
        try:
            await self._take_lock()
        except BaseException:
            await self._lock_conn.close()
            self._lock_conn = None
            raise

        self._pool = await asyncpg.create_pool(
            self._url, min_size=1, max_size=5, command_timeout=15,
            max_inactive_connection_lifetime=300, server_settings=settings,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info(f"Database ready (schema {self._schema})")

    async def _take_lock(self) -> None:
        key = _lock_key(self._schema)
        deadline = time.monotonic() + self.LOCK_WAIT_S
        waiting = False
        while not await self._lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", key):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"another DTEK service instance already holds the lock for schema {self._schema!r}")
            if not waiting:
                waiting = True
                logger.warning(f"The instance lock for schema {self._schema!r} is held by {await self._lock_holder(key)}; "
                               f"waiting up to {self.LOCK_WAIT_S:.0f}s for it")
            await asyncio.sleep(self.LOCK_POLL_S)
        if waiting:
            logger.info("Took the instance lock")

    async def _lock_holder(self, key: int) -> str:
        """Who holds the lock, for the log: an old session of a vanished instance shows a long idle time."""
        try:
            row = await self._lock_conn.fetchrow(
                """
                SELECT a.pid, a.application_name, a.client_addr, a.client_port, a.backend_start, a.state_change
                  FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
                 WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
                   AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
                   AND l.classid = $1 AND l.objid = $2
                """,
                (key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF,
            )
        except Exception as e:
            return f"a session not found ({type(e).__name__}: {e})"
        if row is None:
            return "a session that has just ended"

        def at(moment: Optional[datetime]) -> str:
            return moment.astimezone(KYIV_TZ).strftime("%d.%m %H:%M:%S") if moment else "?"

        return (f"pid {row['pid']} ({row['application_name'] or 'no name'} from "
                f"{row['client_addr']}:{row['client_port']}, connected {at(row['backend_start'])}, "
                f"last active {at(row['state_change'])})")

    async def lock_alive(self) -> bool:
        """False once the connection holding the instance lock is gone."""
        if self._lock_conn is None or self._lock_conn.is_closed():
            return False
        try:
            await asyncio.wait_for(self._lock_conn.fetchval("SELECT 1"), timeout=10)
            return True
        except Exception as e:
            logger.error(f"Instance lock connection failed: {e}")
            return False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PgTx]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield PgTx(conn)

    async def load_source_state(self, source: str) -> Optional[SourceState]:
        assert self._pool is not None
        row = await self._pool.fetchrow(
            "SELECT snapshot, baseline, last_success_at FROM dtek_source_state WHERE source = $1", source
        )
        return _source_state(row)

    async def due_messages(self, limit: int) -> list[OutboxItem]:
        assert self._pool is not None
        rows = await self._pool.fetch(
            """
            SELECT id, bot, chat_id, text, created_at, expires_at, next_attempt_at, attempts
              FROM notification_outbox
             WHERE sent_at IS NULL AND failed_at IS NULL
             ORDER BY id
             LIMIT $1
            """,
            limit,
        )
        return [OutboxItem(**dict(r)) for r in rows]

    async def mark_sent(self, item_id: int, at: datetime) -> None:
        assert self._pool is not None
        await self._pool.execute(
            "UPDATE notification_outbox SET sent_at = $2, attempts = attempts + 1, last_error = NULL WHERE id = $1",
            item_id, at,
        )

    async def mark_retry(self, item_id: int, error: str, next_attempt_at: datetime) -> None:
        assert self._pool is not None
        await self._pool.execute(
            "UPDATE notification_outbox SET attempts = attempts + 1, last_error = $2, next_attempt_at = $3 WHERE id = $1",
            item_id, error[:1000], next_attempt_at,
        )

    async def mark_failed(self, item_id: int, error: str, at: datetime) -> None:
        assert self._pool is not None
        await self._pool.execute(
            "UPDATE notification_outbox SET failed_at = $3, last_error = $2, attempts = attempts + 1 WHERE id = $1",
            item_id, error[:1000], at,
        )

    async def purge_finished(self, before: datetime) -> int:
        assert self._pool is not None
        status = await self._pool.execute(
            "DELETE FROM notification_outbox WHERE sent_at < $1 OR failed_at < $1", before
        )
        return int(status.split()[-1])

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        if self._lock_conn is not None and not self._lock_conn.is_closed():
            await self._lock_conn.close()
        self._lock_conn = None
