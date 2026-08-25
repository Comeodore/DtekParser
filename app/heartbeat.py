"""Drop-in heartbeat client for bots / schedulers — stdlib only, no deps.

Copy this file into each bot project. Configure via env:
    HEARTBEAT_URL    e.g. http://192.168.0.185:8099   (LAN address of the monitor)
    HEARTBEAT_TOKEN  the PUSH_TOKEN from the monitor's .env
    HEARTBEAT_ID     the service id the monitor expects (must match services.yml)

Two ways to use it:

    from heartbeat import start_heartbeat, beat

    # 1) liveness — proves the process/event-loop is alive (background thread)
    start_heartbeat(period=45)

    # 2) functional — call after each real unit of work (handled update / parse)
    beat("parsed 12 rows")

Both are best-effort: network errors are swallowed so the heartbeat can never
break the bot. A missing beat is exactly the signal we want the monitor to see.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.parse
import urllib.request


def _push(service_id: str, base: str, token: str, msg: str) -> None:
    params = {k: v for k, v in {"token": token, "msg": msg}.items() if v}
    url = f"{base.rstrip('/')}/push/{urllib.parse.quote(service_id)}?{urllib.parse.urlencode(params)}"
    urllib.request.urlopen(url, timeout=5).read()


def beat(msg: str = "", *, service_id: str | None = None,
         base: str | None = None, token: str | None = None) -> None:
    """Send a single heartbeat. Silent on failure."""
    service_id = service_id or os.environ.get("HEARTBEAT_ID")
    base = base or os.environ.get("HEARTBEAT_URL")
    token = token or os.environ.get("HEARTBEAT_TOKEN", "")
    if not service_id or not base:
        return
    try:
        _push(service_id, base, token, msg)
    except Exception:
        pass


def start_heartbeat(period: int = 45, *, service_id: str | None = None,
                    base: str | None = None, token: str | None = None) -> threading.Thread:
    """Start a daemon thread that beats every `period` seconds."""
    def loop():
        while True:
            beat("alive", service_id=service_id, base=base, token=token)
            time.sleep(period)

    t = threading.Thread(target=loop, name="heartbeat", daemon=True)
    t.start()
    return t
