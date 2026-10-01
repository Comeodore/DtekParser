"""In-memory Store with real transaction semantics, for tests and local runs."""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import AsyncIterator, Optional, Sequence

from app.notify.router import OutgoingMessage
from app.power.state import PowerState
from app.storage.store import OutboxItem, SourceState


@dataclass
class _Data:
    sources: dict[str, SourceState] = field(default_factory=dict)
    legacy: dict[int, dict] = field(default_factory=dict)
    power: Optional[PowerState] = None
    outbox: dict[int, dict] = field(default_factory=dict)
    next_id: int = 1


class MemoryTx:
    def __init__(self, data: _Data):
        self.data = data

    async def load_source_state(self, source: str) -> Optional[SourceState]:
        return self.data.sources.get(source)

    async def save_source_state(self, source: str, snapshot: dict, baseline: dict, success_at: datetime) -> None:
        self.data.sources[source] = SourceState(copy.deepcopy(snapshot), copy.deepcopy(baseline), success_at)

    async def save_legacy_schedule(self, schedule_id: int, row: dict) -> None:
        self.data.legacy[schedule_id] = copy.deepcopy(row)

    async def load_power_state(self) -> Optional[PowerState]:
        return self.data.power

    async def save_power_state(self, state: PowerState) -> None:
        self.data.power = state

    async def enqueue(self, messages: Sequence[OutgoingMessage]) -> None:
        now = datetime.now(timezone.utc)
        for m in messages:
            self.data.outbox[self.data.next_id] = {
                "item": OutboxItem(self.data.next_id, m.bot, m.chat_id, m.text, now, m.expires_at, None, 0),
                "sent_at": None, "failed_at": None, "error": None,
            }
            self.data.next_id += 1


class MemoryStore:
    def __init__(self) -> None:
        self.data = _Data()
        self._lock = asyncio.Lock()
        self.fail_next_commit = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryTx]:
        async with self._lock:
            working = copy.deepcopy(self.data)
            yield MemoryTx(working)
            if self.fail_next_commit:
                self.fail_next_commit = False
                raise ConnectionError("simulated commit failure")
            self.data = working

    async def load_source_state(self, source: str) -> Optional[SourceState]:
        return self.data.sources.get(source)

    async def due_messages(self, limit: int) -> list[OutboxItem]:
        pending = [r["item"] for r in self.data.outbox.values() if r["sent_at"] is None and r["failed_at"] is None]
        return sorted(pending, key=lambda i: i.id)[:limit]

    async def mark_sent(self, item_id: int, at: datetime) -> None:
        row = self.data.outbox[item_id]
        row["sent_at"] = at
        row["item"] = replace(row["item"], attempts=row["item"].attempts + 1)

    async def mark_retry(self, item_id: int, error: str, next_attempt_at: datetime) -> None:
        row = self.data.outbox[item_id]
        row["error"] = error
        row["item"] = replace(row["item"], attempts=row["item"].attempts + 1, next_attempt_at=next_attempt_at)

    async def mark_failed(self, item_id: int, error: str, at: datetime) -> None:
        row = self.data.outbox[item_id]
        row["failed_at"], row["error"] = at, error

    async def purge_finished(self, before: datetime) -> int:
        doomed = [i for i, r in self.data.outbox.items()
                  if (r["sent_at"] and r["sent_at"] < before) or (r["failed_at"] and r["failed_at"] < before)]
        for i in doomed:
            del self.data.outbox[i]
        return len(doomed)

    # helpers for tests
    def sent_texts(self, chat_id: Optional[str] = None) -> list[str]:
        return [r["item"].text for r in sorted(self.data.outbox.values(), key=lambda r: r["item"].id)
                if r["sent_at"] and (chat_id is None or r["item"].chat_id == chat_id)]

    def queued(self) -> list[OutboxItem]:
        return [r["item"] for r in sorted(self.data.outbox.values(), key=lambda r: r["item"].id)]
