from datetime import timedelta

import pytest

import httpx
from telegram.error import TimedOut

from app.notify.outbox import MaybeDelivered, OutboxSender, PermanentSendError, RetryLater, TelegramGateway
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


async def test_unanswered_send_is_not_repeated_and_does_not_block_the_chat(env):
    # 18.11.2026: Telegram delivered requests whose answer timed out a minute or two later, so a retry doubled them.
    store, gateway, clock, sender = env
    gateway.failures["a"] = [MaybeDelivered("no answer")]
    await enqueue(store, ("1", "a"), ("1", "b"))
    assert await sender.flush() == 2
    assert gateway.sent == [("bot", "1", "b")]
    assert store.sent_texts("1") == ["a", "b"]
    assert store.data.outbox[1]["error"] == "unconfirmed: no answer"
    clock["now"] = T0 + timedelta(minutes=10)
    assert await sender.flush() == 0


def _timed_out(cause: Exception) -> TimedOut:
    try:
        raise TimedOut() from cause
    except TimedOut as e:
        return e


class FakeBot:
    def __init__(self, error: Exception):
        self.error = error

    async def send_message(self, **kwargs):
        raise self.error


@pytest.mark.parametrize("cause, expected", [
    (httpx.ReadTimeout("read"), MaybeDelivered),
    (httpx.ConnectTimeout("connect"), RetryLater),
    (httpx.WriteTimeout("write"), RetryLater),
    (httpx.PoolTimeout("pool"), RetryLater),
])
async def test_gateway_tells_a_lost_answer_from_a_request_never_taken(cause, expected):
    gateway = TelegramGateway({"bot": "token"})
    gateway._bots["bot"] = FakeBot(_timed_out(cause))
    with pytest.raises(expected):
        await gateway.send("bot", "1", "a")


async def test_gateway_retries_a_read_timeout_while_initialising(monkeypatch):
    # getMe timing out sent nothing to the chat.
    from telegram import Bot

    async def initialize(self):
        raise _timed_out(httpx.ReadTimeout("read"))

    monkeypatch.setattr(Bot, "initialize", initialize)
    with pytest.raises(RetryLater):
        await TelegramGateway({"bot": "123:abc"}).send("bot", "1", "a")


async def test_gateway_sees_the_read_timeout_through_python_telegram_bot(monkeypatch):
    from telegram import Bot
    from telegram.request import HTTPXRequest

    async def request(self, *args, **kwargs):
        raise httpx.ReadTimeout("read")

    async def no_init(self):
        pass

    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    monkeypatch.setattr(Bot, "initialize", no_init)
    gateway = TelegramGateway({"bot": "123:abc"})
    gateway._bots["bot"] = Bot("123:abc", request=HTTPXRequest())
    with pytest.raises(MaybeDelivered):
        await gateway.send("bot", "1", "a")
