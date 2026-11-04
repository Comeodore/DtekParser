"""Who gets which message: turns events into outbox rows."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from app.config import Settings, SourceConfig
from app.dtek.models import Outage, OutageKind, Snapshot
from app.engine.events import Event, OutageChange, ScheduleEvent
from app.notify.messages import render_outage, render_power, render_schedule
from app.power.state import PowerTransition

logger = logging.getLogger(__name__)

# Undelivered messages older than this are dropped rather than sent stale.
MESSAGE_TTL = timedelta(hours=6)
# DTEK's own news is not kept that long: once the service is back, its next cycle reports the difference as it
# is then, and the reserve on Cloudflare (edge/) has covered the time in between.
EVENT_TTL = timedelta(minutes=15)
# Outage notices worth a message; schedule-driven ones are covered by schedule messages.
NOTIFIABLE_KINDS = (OutageKind.EMERGENCY, OutageKind.PLANNED)


@dataclass(frozen=True)
class OutgoingMessage:
    bot: str
    chat_id: str
    text: str
    expires_at: datetime


def _notifiable(outage: Optional[Outage], scheduled: bool = False) -> bool:
    return outage is not None and (scheduled or outage.kind in NOTIFIABLE_KINDS or outage.schedules_suspended)


def should_send(event: Event, source: SourceConfig, scheduled: bool = False) -> bool:
    """`scheduled`: the notices of outages by the schedule as well, which this service leaves to the mini app;
    the reserve sends them for kem while it is in charge and the mini app is down (edge/worker.js)."""
    if isinstance(event, ScheduleEvent):
        return True
    if not source.notify_outages:
        return False
    if event.change is OutageChange.STARTED:
        return _notifiable(event.outage, scheduled)
    return _notifiable(event.outage, scheduled) or _notifiable(event.previous, scheduled)


def event_texts(source: SourceConfig, events: Iterable[Event], snapshot: Snapshot, now: datetime,
                scheduled: bool = False) -> list[str]:
    """What a source's chats get for these events. Also run by the reserve's dtek-brain, which passes only
    the name, lang and notify_outages of the source."""
    texts: list[str] = []
    for event in events:
        if not should_send(event, source, scheduled):
            logger.info(f"[{source.name}] not notifying {event}")
            continue
        if isinstance(event, ScheduleEvent):
            texts.append(render_schedule(event, snapshot, now, source.lang))
        else:
            texts.append(render_outage(event, snapshot, now, source.lang))
    return texts


class Router:
    def __init__(self, settings: Settings):
        self._settings = settings

    def _fan_out(self, source: SourceConfig, text: str, now: datetime,
                 ttl: timedelta = MESSAGE_TTL) -> list[OutgoingMessage]:
        expires = now + ttl
        return [OutgoingMessage(source.bot, chat, text, expires) for chat in source.chat_ids]

    def for_events(self, source: SourceConfig, events: Iterable[Event], snapshot: Snapshot,
                   now: datetime) -> list[OutgoingMessage]:
        messages: list[OutgoingMessage] = []
        for text in event_texts(source, events, snapshot, now):
            messages.extend(self._fan_out(source, text, now, EVENT_TTL))
        return messages

    def for_power(self, transition: PowerTransition, snapshot: Optional[Snapshot],
                  now: datetime) -> list[OutgoingMessage]:
        assert self._settings.power is not None
        return self.for_grid(self._settings.power.source, transition, snapshot, now)

    def for_grid(self, source_name: str, transition: PowerTransition, snapshot: Optional[Snapshot],
                 now: datetime) -> list[OutgoingMessage]:
        """Power lost or back at a source's address, however it was noticed (HA voltage, Deye grid)."""
        source = self._settings.source(source_name)
        return self._fan_out(source, render_power(transition, snapshot, now, source.lang), now)
