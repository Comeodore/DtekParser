from datetime import timedelta

import pytest

from app.notify.outbox import OutboxSender, PermanentSendError, RetryLater
from app.notify.router import OutgoingMessage
from app.storage.memory import MemoryStore
from tests.conftest import kyiv

T0 = kyiv(2026, 10, 1, 12)


class FakeGateway:
    def __init__(self):
        self.sent = []
        self.failures = {}   # text -> list of exceptions to raise, in order

    async def send(self, bot, chat_id, text):
        queue = self.failures.get(text)
        if queue:
            raise queue.pop(0)
        self.sent.append((bot, chat_id, text))


@pytest.fixture
def env():
    store, gateway, clock = MemoryStore(), FakeGateway(), {"now": T0}
    sender = OutboxSender(store, gateway, clock=lambda: clock["now"])
    return store, gateway, clock, sender


async def enqueue(store, *items, expires=T0 + timedelta(hours=6)):
    async with store.transaction() as tx:
        await tx.enqueue([OutgoingMessage("bot", chat, text, expires) for chat, text in items])


async def test_delivers_in_order(env):
    store, gateway, _, sender = env
    await enqueue(store, ("1", "a"), ("2", "b"), ("1", "c"))
    assert await sender.flush() == 3
    assert [x[2] for x in gateway.sent] == ["a", "b", "c"]
    assert await sender.flush() == 0


async def test_transient_failure_blocks_only_that_chat_and_keeps_order(env):
    store, gateway, clock, sender = env
    gateway.failures["a"] = [RetryLater("network down")]
    await enqueue(store, ("1", "a"), ("1", "b"), ("2", "x"))
    assert await sender.flush() == 1
    assert gateway.sent == [("bot", "2", "x")]

    assert await sender.flush() == 0          # still backing off
    clock["now"] = T0 + timedelta(seconds=30)
    assert await sender.flush() == 2
    assert [x[2] for x in gateway.sent] == ["x", "a", "b"]


async def test_rate_limit_waits_as_told(env):
    store, gateway, clock, sender = env
    gateway.failures["a"] = [RetryLater("flood", delay=120)]
    await enqueue(store, ("1", "a"))
    await sender.flush()
    clock["now"] = T0 + timedelta(seconds=60)
    assert await sender.flush() == 0
    clock["now"] = T0 + timedelta(seconds=121)
    assert await sender.flush() == 1


async def test_permanent_failure_does_not_block_the_chat(env):
    store, gateway, _, sender = env
    gateway.failures["a"] = [PermanentSendError("Forbidden: bot was blocked")]
    await enqueue(store, ("1", "a"), ("1", "b"))
    assert await sender.flush() == 1
    assert gateway.sent == [("bot", "1", "b")]
    assert await store.due_messages(10) == []


async def test_stale_messages_are_dropped(env):
    store, gateway, clock, sender = env
    await enqueue(store, ("1", "a"), expires=T0 + timedelta(minutes=1))
    clock["now"] = T0 + timedelta(minutes=2)
    assert await sender.flush() == 0
    assert gateway.sent == [] and await store.due_messages(10) == []


async def test_fresh_message_is_due_regardless_of_clock_skew(env):
    # The database's clock ran ~80 ms ahead in staging; a fresh row must still go out at once.
    store, gateway, clock, sender = env
    await enqueue(store, ("1", "a"))
    clock["now"] = T0 - timedelta(hours=1)
    assert await sender.flush() == 1
