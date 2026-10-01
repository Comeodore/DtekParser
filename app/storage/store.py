"""Persistence interface. Everything a cycle changes is written in one transaction."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import AsyncContextManager, Optional, Protocol, Sequence

from app.notify.router import OutgoingMessage
from app.power.state import PowerState


@dataclass(frozen=True)
class SourceState:
    snapshot: Optional[dict]
    baseline: Optional[dict]
    last_success_at: Optional[datetime]


@dataclass(frozen=True)
class OutboxItem:
    id: int
    bot: str
    chat_id: str
    text: str
    created_at: datetime
    expires_at: datetime
    next_attempt_at: Optional[datetime]   # None: due now
    attempts: int


class Tx(Protocol):
    async def load_source_state(self, source: str) -> Optional[SourceState]: ...
    async def save_source_state(self, source: str, snapshot: dict, baseline: dict, success_at: datetime) -> None: ...
    async def save_legacy_schedule(self, schedule_id: int, row: dict) -> None: ...
    async def load_power_state(self) -> Optional[PowerState]: ...
    async def save_power_state(self, state: PowerState) -> None: ...
    async def enqueue(self, messages: Sequence[OutgoingMessage]) -> None: ...


class Store(Protocol):
    def transaction(self) -> AsyncContextManager[Tx]: ...
    async def load_source_state(self, source: str) -> Optional[SourceState]: ...
    async def due_messages(self, limit: int) -> list[OutboxItem]: ...
    async def mark_sent(self, item_id: int, at: datetime) -> None: ...
    async def mark_retry(self, item_id: int, error: str, next_attempt_at: datetime) -> None: ...
    async def mark_failed(self, item_id: int, error: str, at: datetime) -> None: ...
    async def purge_finished(self, before: datetime) -> int: ...
