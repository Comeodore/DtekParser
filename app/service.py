"""Wires the components together and keeps their loops running."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import datetime
from typing import Awaitable, Callable, Optional

from app.collector import SourceCollector
from app.config import Settings
from app.dtek.client import BrowserPool, DtekSite
from app.heartbeat import Heartbeat
from app.notify.outbox import Gateway, OutboxSender, TelegramGateway
from app.notify.router import Router
from app.power.monitor import PowerMonitor
from app.storage.postgres import PostgresStore
from app.timeutil import KYIV_TZ, now_kyiv

logger = logging.getLogger(__name__)


async def supervise(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    """Restart a loop that crashed, with backoff; a loop is never allowed to die silently."""
    delay = 5.0
    while True:
        try:
            await factory()
            logger.error(f"{name} returned unexpectedly, restarting")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"{name} crashed, restarting in {delay:.0f}s")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300.0)


class Service:
    LOCK_CHECK_S = 30.0
    HEARTBEAT_ID = "dtek-service"
    HEARTBEAT_EVERY_S = 30.0
    OUTBOX_STALE_S = 60.0

    def __init__(self, settings: Settings, store=None, gateway: Optional[Gateway] = None):
        self.settings = settings
        self.store = store if store is not None else PostgresStore(settings.database_url, settings.db_schema)
        self.router = Router(settings)
        self.gateway = gateway if gateway is not None else TelegramGateway(settings.bots, settings.dry_run)
        self.outbox = OutboxSender(self.store, self.gateway)
        self.pool = BrowserPool()

        self.sites: dict[str, DtekSite] = {}
        self.collectors: dict[str, SourceCollector] = {}
        for cfg in settings.sources:
            site = DtekSite(self.pool, cfg.name, cfg.url, cfg.street, cfg.settlement,
                            state_path=os.path.join(settings.state_dir, f"state-{cfg.name}.json"))
            self.sites[cfg.name] = site
            self.collectors[cfg.name] = SourceCollector(
                cfg, site, self.store, self.router, settings.fetch_interval, on_messages=self.outbox.wake,
            )

        self.power: Optional[PowerMonitor] = None
        if settings.power is not None:
            power_source = self.collectors[settings.power.source]
            self.power = PowerMonitor(
                settings.power, self.store, self.router, snapshot=lambda: power_source.latest,
                on_messages=self.outbox.wake,
            )
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.store.connect()
        for collector in self.collectors.values():
            await collector.restore()
        if self.power is not None:
            await self.power.restore()

        self._spawn("outbox", self.outbox.run)
        for collector in self.collectors.values():
            self._spawn(f"collector[{collector.name}]", collector.run)
        if self.power is not None:
            self._spawn("power", self.power.run)
        self._spawn("heartbeat", self._heartbeat)
        if isinstance(self.store, PostgresStore):
            self._spawn("instance-lock", self._watch_lock)
        logger.info(f"Service started: sources={list(self.collectors)}, power={'on' if self.power else 'off'}, "
                    f"dry_run={self.settings.dry_run}")

    def problems(self, now: Optional[datetime] = None) -> list[str]:
        """Why the service is not fully working; empty when it is. Drives the heartbeat and /health."""
        now = now or now_kyiv()
        found = []
        for c in self.collectors.values():
            if not c.healthy(now):
                since = c.last_success.astimezone(KYIV_TZ).strftime("%d.%m %H:%M") if c.last_success else "start"
                detail = f": {c.last_error}" if c.last_error else ""
                found.append(f"{c.name}: no successful DTEK fetch since {since}{detail}")
        if self.power is not None and not self.power.connected:
            found.append("power: Home Assistant is not connected")
        if self.outbox.last_cycle is None or time.monotonic() - self.outbox.last_cycle > self.OUTBOX_STALE_S:
            found.append("outbox: message delivery loop is not cycling")
        return found

    async def _heartbeat(self) -> None:
        """One heartbeat for the whole service, sent only while nothing is wrong."""
        beat = Heartbeat(self.HEARTBEAT_ID, min_interval=0)
        reported: Optional[list[str]] = None
        # Let the first fetches and the HA connection happen before judging.
        await asyncio.sleep(self.HEARTBEAT_EVERY_S)
        while True:
            problems = self.problems()
            if problems:
                if problems != reported:
                    logger.warning("Heartbeat paused: " + "; ".join(problems))
            else:
                beat.pulse("ok")
                if reported:
                    logger.info("All checks pass again, heartbeat resumed")
            reported = problems
            await asyncio.sleep(self.HEARTBEAT_EVERY_S)

    def _spawn(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        self._tasks.append(asyncio.create_task(supervise(name, factory), name=name))

    async def _watch_lock(self) -> None:
        while True:
            await asyncio.sleep(self.LOCK_CHECK_S)
            if not await self.store.lock_alive():
                logger.critical("Lost the single-instance lock; stopping so the restart can take it again")
                os.kill(os.getpid(), signal.SIGTERM)
                return

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # One last delivery attempt for anything queued during shutdown.
        try:
            await asyncio.wait_for(self.outbox.flush(), timeout=10)
        except Exception as e:
            logger.warning(f"Final outbox flush failed: {e}")
        for site in self.sites.values():
            await site.close()
        await self.pool.close()
        if isinstance(self.gateway, TelegramGateway):
            await self.gateway.close()
        await self.store.close()
        logger.info("Service stopped")
