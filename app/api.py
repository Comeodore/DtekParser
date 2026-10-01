"""HTTP API and the Telegram mini app. Each source's port serves that source."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from app.collector import SourceCollector
from app.dtek.legacy import legacy_row
from app.service import Service
from app.timeutil import KYIV_TZ, now_kyiv
from app.wol import send_magic_packet

logger = logging.getLogger(__name__)

APP_VERSION = "3.0.0"
STATIC_DIR = Path(__file__).parent / "static"
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


def schedule_payload(collector: SourceCollector, now: datetime) -> dict:
    cfg = collector.config
    payload = {
        "source": cfg.name,
        "parser_type": cfg.name,
        "lang": cfg.lang,
        "healthy": collector.healthy(now),
        "updated_at": collector.last_success.isoformat() if collector.last_success else None,
        "current_outage": None,
        "today": None,
        "tomorrow": None,
        "outage": None,
        "days": [],
        "schedule_updated": "",
        "outage_updated": "",
        "availability": None,
        "schedules_suspended": False,
    }
    snapshot = collector.latest
    if snapshot is None:
        return payload
    today = now.astimezone(KYIV_TZ).date()
    legacy = legacy_row(snapshot, today)
    payload.update(
        current_outage=legacy["current_outage"],
        today=legacy["today"],
        tomorrow=legacy["tomorrow"],
        outage=snapshot.outage.to_dict() if snapshot.outage else None,
        days=[
            {"date": d.date.isoformat(), "label": d.date.strftime("%d.%m.%y"),
             "is_today": d.date == today, "lines": [line.to_dict() for line in d.lines]}
            for d in snapshot.days
            if today <= d.date <= today + timedelta(days=1)
        ],
        schedule_updated=snapshot.schedule_updated,
        outage_updated=snapshot.outage_updated,
        availability=snapshot.availability.value,
        schedules_suspended=snapshot.schedules_suspended,
    )
    return payload


def create_app(service: Service) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="DTEK service", version=APP_VERSION, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])
    by_port = {c.config.port: c for c in service.collectors.values()}

    def resolve(request: Request, source: Optional[str]) -> SourceCollector:
        if source:
            collector = service.collectors.get(source)
            if collector is None:
                raise HTTPException(404, f"unknown source {source!r}")
            return collector
        server = request.scope.get("server") or (None, None)
        return by_port.get(server[1]) or next(iter(service.collectors.values()))

    @app.get("/", response_class=HTMLResponse)
    async def webapp():
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={**NO_CACHE, "X-App-Version": APP_VERSION})

    @app.get("/api")
    async def info():
        return {"service": "DTEK service", "version": APP_VERSION}

    @app.get("/api/v1/schedule")
    async def schedule(request: Request, source: Optional[str] = Query(None)):
        return JSONResponse(schedule_payload(resolve(request, source), now_kyiv()), headers=NO_CACHE)

    @app.get("/api/v1/schedule/today")
    async def schedule_today(request: Request, source: Optional[str] = Query(None)):
        return schedule_payload(resolve(request, source), now_kyiv())["today"]

    @app.get("/api/v1/schedule/tomorrow")
    async def schedule_tomorrow(request: Request, source: Optional[str] = Query(None)):
        return schedule_payload(resolve(request, source), now_kyiv())["tomorrow"]

    @app.get("/api/v1/status")
    async def status(request: Request, source: Optional[str] = Query(None)):
        return schedule_payload(resolve(request, source), now_kyiv())["current_outage"]

    @app.get("/api/v1/health")
    async def health():
        now = now_kyiv()
        sources = {
            name: {
                "healthy": c.healthy(now),
                "last_success": c.last_success.isoformat() if c.last_success else None,
                "failures": c.failures,
                "last_error": c.last_error or None,
            }
            for name, c in service.collectors.items()
        }
        power = None
        if service.power is not None:
            state = service.power.state
            power = {
                "connected": service.power.connected,
                "status": state.status.name if state else None,
                "since": state.since.isoformat() if state else None,
            }
        problems = service.problems(now)
        ok = not problems
        body = {"status": "ok" if ok else "degraded", "version": APP_VERSION, "problems": problems,
                "sources": sources, "power": power}
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.post("/api/v1/wol")
    async def wake_on_lan(request: Request):
        mac = service.settings.wol_mac
        if not mac:
            raise HTTPException(404, "Wake-on-LAN is not configured")
        client = request.client.host if request.client else "?"
        via = "cloudflare" if request.headers.get("cf-connecting-ip") else "direct"
        try:
            await asyncio.to_thread(send_magic_packet, mac, service.settings.wol_broadcast)
        except OSError as e:
            logger.error(f"WoL to {mac} failed (requested by {client}, {via}): {e}")
            return {"status": "error", "detail": str(e)}
        logger.info(f"WoL packet sent to {mac} (requested by {client}, {via})")
        return {"status": "ok"}

    return app
