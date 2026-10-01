"""Delivers queued messages to Telegram, at least once and in order per chat."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Mapping, Optional, Protocol

from app.storage.store import Store
from app.timeutil import now_kyiv

logger = logging.getLogger(__name__)


class PermanentSendError(Exception):
    """Retrying cannot help (bot blocked, chat gone, malformed message)."""


class RetryLater(Exception):
    def __init__(self, message: str, delay: Optional[float] = None):
        super().__init__(message)
        self.delay = delay


class Gateway(Protocol):
    async def send(self, bot: str, chat_id: str, text: str) -> None: ...


class TelegramGateway:
    def __init__(self, tokens: Mapping[str, str], dry_run: bool = False):
        self._tokens = dict(tokens)
        self._dry_run = dry_run
        self._bots: dict = {}

    async def _bot(self, name: str):
        from telegram import Bot
        from telegram.request import HTTPXRequest

        if name not in self._bots:
            if name not in self._tokens:
                raise PermanentSendError(f"no token for bot {name!r}")
            bot = Bot(self._tokens[name], request=HTTPXRequest(connect_timeout=10, read_timeout=20, write_timeout=20))
            await bot.initialize()
            self._bots[name] = bot
        return self._bots[name]

    async def send(self, bot: str, chat_id: str, text: str) -> None:
        from telegram.error import BadRequest, ChatMigrated, Forbidden, InvalidToken, RetryAfter, TelegramError

        if self._dry_run:
            logger.info(f"[dry-run] {bot} -> {chat_id}:\n{text}")
            return
        try:
            client = await self._bot(bot)
            await client.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except RetryAfter as e:
            delay = e.retry_after.total_seconds() if isinstance(e.retry_after, timedelta) else float(e.retry_after)
            raise RetryLater(f"rate limited: {e}", delay) from e
        except (Forbidden, BadRequest, ChatMigrated) as e:
            raise PermanentSendError(f"{type(e).__name__}: {e}") from e
        except InvalidToken as e:
            # Not the message's fault; keep it until the token is fixed or it expires.
            self._bots.pop(bot, None)
            raise RetryLater(f"invalid token for {bot}", 300) from e
        except TelegramError as e:
            # Covers TimedOut/NetworkError; a bot that failed to initialize was not cached.
            raise RetryLater(f"{type(e).__name__}: {e}") from e

    async def close(self) -> None:
        for bot in self._bots.values():
            try:
                await bot.shutdown()
            except Exception as e:
                logger.warning(f"Error closing Telegram bot: {e}")
        self._bots.clear()


def backoff(attempts: int) -> float:
    return float(min(10 * 2 ** attempts, 600))


class OutboxSender:
    BATCH = 100
    POLL_SECONDS = 5.0
    KEEP_FINISHED = timedelta(days=30)
    PURGE_EVERY = timedelta(hours=1)

    def __init__(self, store: Store, gateway: Gateway, clock: Callable[[], datetime] = now_kyiv,
                 on_cycle: Optional[Callable[[], None]] = None):
        self._store = store
        self._gateway = gateway
        self._clock = clock
        self._on_cycle = on_cycle
        self._wake = asyncio.Event()
        self._last_purge: Optional[datetime] = None

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await self.flush()
                await self._purge_if_due()
                if self._on_cycle:
                    self._on_cycle()
            except Exception as e:
                logger.error(f"Outbox cycle failed: {e}")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def flush(self) -> int:
        sent = 0
        blocked: set[tuple[str, str]] = set()
        for item in await self._store.due_messages(self.BATCH):
            chat = (item.bot, item.chat_id)
            if chat in blocked:
                continue
            now = self._clock()
            if item.expires_at <= now:
                logger.error(f"Dropping expired message #{item.id} to {item.chat_id} (created {item.created_at})")
                await self._store.mark_failed(item.id, "expired", now)
                continue
            if item.next_attempt_at is not None and item.next_attempt_at > now:
                blocked.add(chat)   # keep the chat's order: nothing jumps ahead of a retry
                continue
            try:
                await self._gateway.send(item.bot, item.chat_id, item.text)
            except PermanentSendError as e:
                logger.error(f"Message #{item.id} to {item.chat_id} failed permanently: {e}")
                await self._store.mark_failed(item.id, str(e), self._clock())
                continue
            except RetryLater as e:
                delay = e.delay if e.delay is not None else backoff(item.attempts)
                logger.warning(f"Message #{item.id} to {item.chat_id}: {e}; retry in {delay:.0f}s")
                await self._store.mark_retry(item.id, str(e), self._clock() + timedelta(seconds=delay))
                blocked.add(chat)
                continue
            except Exception as e:
                delay = backoff(item.attempts)
                logger.warning(f"Message #{item.id} to {item.chat_id}: unexpected {type(e).__name__}: {e}; "
                               f"retry in {delay:.0f}s")
                await self._store.mark_retry(item.id, f"{type(e).__name__}: {e}", self._clock() + timedelta(seconds=delay))
                blocked.add(chat)
                continue
            await self._store.mark_sent(item.id, self._clock())
            logger.info(f"📤 Message #{item.id} sent to {item.chat_id} via {item.bot}")
            sent += 1
        return sent

    async def _purge_if_due(self) -> None:
        now = self._clock()
        if self._last_purge and now - self._last_purge < self.PURGE_EVERY:
            return
        self._last_purge = now
        removed = await self._store.purge_finished(now - self.KEEP_FINISHED)
        if removed:
            logger.info(f"Purged {removed} old outbox rows")
