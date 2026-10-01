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
# Outage notices worth a message; schedule-driven ones are covered by schedule messages.
NOTIFIABLE_KINDS = (OutageKind.EMERGENCY, OutageKind.PLANNED)


@dataclass(frozen=True)
class OutgoingMessage:
    bot: str
    chat_id: str
    text: str
    expires_at: datetime


def _notifiable(outage: Optional[Outage]) -> bool:
    return outage is not None and (outage.kind in NOTIFIABLE_KINDS or outage.schedules_suspended)


def should_send(event: Event, source: SourceConfig) -> bool:
    if isinstance(event, ScheduleEvent):
        return True
    if not source.notify_outages:
        return False
    if event.change is OutageChange.STARTED:
        return _notifiable(event.outage)
    return _notifiable(event.outage) or _notifiable(event.previous)


class Router:
    def __init__(self, settings: Settings):
        self._settings = settings

    def _fan_out(self, source: SourceConfig, text: str, now: datetime) -> list[OutgoingMessage]:
        expires = now + MESSAGE_TTL
        return [OutgoingMessage(source.bot, chat, text, expires) for chat in source.chat_ids]

    def for_events(self, source: SourceConfig, events: Iterable[Event], snapshot: Snapshot,
                   now: datetime) -> list[OutgoingMessage]:
        messages: list[OutgoingMessage] = []
        for event in events:
            if not should_send(event, source):
                logger.info(f"[{source.name}] not notifying {event}")
                continue
            if isinstance(event, ScheduleEvent):
                text = render_schedule(event, snapshot, now, source.lang)
            else:
                text = render_outage(event, snapshot, now, source.lang)
            messages.extend(self._fan_out(source, text, now))
        return messages

    def for_power(self, transition: PowerTransition, snapshot: Optional[Snapshot],
                  now: datetime) -> list[OutgoingMessage]:
        assert self._settings.power is not None
        source = self._settings.source(self._settings.power.source)
        return self._fan_out(source, render_power(transition, snapshot, now, source.lang), now)
