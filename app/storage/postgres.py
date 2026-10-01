from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Optional, Sequence

import asyncpg

from app.notify.router import OutgoingMessage
from app.power.state import PowerState, PowerStatus
from app.storage.store import OutboxItem, SourceState

logger = logging.getLogger(__name__)

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

    async def load_power_state(self) -> Optional[PowerState]:
        row = await self._conn.fetchrow("SELECT state, timestamp FROM power_events WHERE id = 1 FOR UPDATE")
        if row is None:
            return None
        return PowerState(PowerStatus(row["state"]), row["timestamp"])

    async def save_power_state(self, state: PowerState) -> None:
        status = await self._conn.execute(
            "UPDATE power_events SET state = $1, timestamp = $2 WHERE id = 1", state.status.value, state.since
        )
        if status == "UPDATE 0":
            await self._conn.execute(
                "INSERT INTO power_events (id, state, timestamp) VALUES (1, $1, $2)", state.status.value, state.since
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
    def __init__(self, url: str, schema: str = "public"):
        self._url = url
        self._schema = schema
        self._pool: Optional[asyncpg.Pool] = None
        self._lock_conn: Optional[asyncpg.Connection] = None

    async def connect(self) -> None:
        settings = {"application_name": "DtekService", "search_path": self._schema}
        bootstrap = await asyncpg.connect(self._url, timeout=15)
        try:
            if self._schema != "public":
                await bootstrap.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
        finally:
            await bootstrap.close()

        # One instance per schema: a second one would send every message twice.
        self._lock_conn = await asyncpg.connect(self._url, timeout=15, server_settings=settings)
        if not await self._lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", _lock_key(self._schema)):
            await self._lock_conn.close()
            raise RuntimeError(f"another DTEK service instance already holds the lock for schema {self._schema!r}")

        self._pool = await asyncpg.create_pool(
            self._url, min_size=1, max_size=5, command_timeout=15,
            max_inactive_connection_lifetime=300, server_settings=settings,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info(f"Database ready (schema {self._schema})")

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
