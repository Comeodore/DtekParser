"""Pushes to the home monitoring stack (nginx :8099 -> VictoriaMetrics).

Beats are functional: a source beats after a successful fetch, the power
monitor while Home Assistant is streaming, the outbox after a healthy cycle.
A missing beat is what raises "Heartbeat missing" in Grafana.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)


def _push(base: str, service_id: str, token: str, msg: str) -> None:
    params = urllib.parse.urlencode({k: v for k, v in {"token": token, "msg": msg}.items() if v})
    url = f"{base.rstrip('/')}/push/{urllib.parse.quote(service_id)}" + (f"?{params}" if params else "")
    urllib.request.urlopen(url, timeout=5).read()


class Heartbeat:
    def __init__(self, service_id: str, min_interval: float = 30.0):
        self.service_id = service_id
        self._min_interval = min_interval
        self._last = 0.0
        self._base = os.environ.get("HEARTBEAT_URL", "")
        self._token = os.environ.get("HEARTBEAT_TOKEN", "")

    def pulse(self, msg: str = "") -> None:
        """Fire-and-forget; never raises, never blocks the loop."""
        if not self._base:
            return
        now = time.monotonic()
        if now - self._last < self._min_interval:
            return
        self._last = now
        try:
            asyncio.get_running_loop().run_in_executor(None, self._send, msg)
        except RuntimeError:
            pass

    def _send(self, msg: str) -> None:
        try:
            _push(self._base, self.service_id, self._token, msg)
        except Exception as e:
            logger.debug(f"Heartbeat {self.service_id} failed: {e}")
