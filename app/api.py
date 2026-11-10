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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app.collector import SourceCollector
from app.inverter.metrics import exposition
from app.page import page_payload, scheduled_status  # noqa: F401  (scheduled_status: tests)
from app.power.verdict import Actual
from app.reserve_hook import charge_table
from app.service import Service
from app.timeutil import KYIV_TZ, now_kyiv
from app.webapp_auth import webapp_user
from app.wol import send_magic_packet

logger = logging.getLogger(__name__)

APP_VERSION = "3.0.0"
STATIC_DIR = Path(__file__).parent / "static"
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


# deye-reserve looks at the battery every 3 min: an estimate older than this is not the present load's.
BATTERY_FRESH = timedelta(minutes=10)


def battery_until(battery: Optional[dict], inverter: dict, now: datetime) -> Optional[str]:
    """"HH:MM" the battery runs down to its shutdown at the present load, once deye-reserve warned that it will
    not last and while the grid is away; None otherwise (the grid back is seen here first)."""
    if not battery or not battery.get("empty_at") or inverter.get("grid_present") is not False:
        return None
    if now - datetime.fromisoformat(battery["at"]) > BATTERY_FRESH:
        return None
    return f"{datetime.fromisoformat(battery['empty_at']).astimezone(KYIV_TZ):%H:%M}"


def schedule_payload(collector: SourceCollector, now: datetime, inverter: Optional[dict] = None,
                     batteries: Optional[list[dict]] = None, actual: Optional[Actual] = None) -> dict:
    cfg = collector.config
    return page_payload(collector.latest, now, cfg.name, cfg.lang, collector.healthy(now), collector.last_success,
                        inverter, batteries, actual)


def problem_exposition(failing: list[tuple[str, str, str]]) -> str:
    """One sample per failing check, labelled with its reason for Grafana's "DTEK service problem" alert; nothing
    while all is well, so the series go stale and the alert resolves."""
    if not failing:
        return ""

    def label(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    lines = ["# HELP dtek_service_problem 1 while this check keeps the service from working fully",
             "# TYPE dtek_service_problem gauge"]
    lines += [f'dtek_service_problem{{check="{label(check)}",reason="{label(reason)}"}} 1'
              for check, reason, _ in failing]
    return "\n".join(lines) + "\n"


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

    def batteries_for(name: str, now: datetime) -> list[dict]:
        """The home battery (HA) first, then the Deye ones, for the source they are shown with."""
        rows = []
        power = service.power
        if power is not None and power.source == name and power.battery_payload() is not None:
            rows.append(power.battery_payload())
        monitor = service.inverter
        if monitor is not None and monitor.config.batteries_source == name:
            rows += monitor.batteries_payload(now)
        return rows

    def actual_for(name: str, now: datetime) -> Optional[Actual]:
        """The power really there: the home's HA sensor, else the building's inverters. Not the krem inverter's
        grid: its mini app keeps DTEK's badge, the diagram shows the grid."""
        found = None
        power, monitor = service.power, service.inverter
        if power is not None and power.source == name:
            found = power.actual(now)
        if found is None and monitor is not None and monitor.config.batteries_source == name:
            found = monitor.batteries_actual(now)
        return found

    @app.get("/api/v1/schedule")
    async def schedule(request: Request, source: Optional[str] = Query(None)):
        collector, now = resolve(request, source), now_kyiv()
        monitor = service.inverter
        inverter = monitor.payload(now) if monitor is not None and monitor.source == collector.name else None
        if inverter is not None:
            sun = await service.reserve.sun()
            if sun and sun.get("date") == now.date().isoformat():
                inverter["pv_forecast_kwh"] = sun.get("p50_kwh")
            inverter["battery_until"] = battery_until(await service.reserve.battery(), inverter, now)
        payload = schedule_payload(collector, now, inverter, batteries_for(collector.name, now),
                                   actual_for(collector.name, now))
        return JSONResponse(payload, headers=NO_CACHE)

    @app.get("/api/v1/schedule/today")
    async def schedule_today(request: Request, source: Optional[str] = Query(None)):
        return schedule_payload(resolve(request, source), now_kyiv())["today"]

    @app.get("/api/v1/schedule/tomorrow")
    async def schedule_tomorrow(request: Request, source: Optional[str] = Query(None)):
        return schedule_payload(resolve(request, source), now_kyiv())["tomorrow"]

    @app.get("/api/v1/status")
    async def status(request: Request, source: Optional[str] = Query(None)):
        return schedule_payload(resolve(request, source), now_kyiv())["current_outage"]

    @app.get("/api/v1/reserve/table")
    async def reserve_table(request: Request, source: Optional[str] = Query(None)):
        """deye-reserve's TOU tables in the inverter's mini app, for the owner only: who it is comes from the
        Telegram initData the page sends (X-Telegram-Init-Data), checked against the source's bot token."""
        collector, now = resolve(request, source), now_kyiv()
        settings = service.settings
        if settings.deye is None or collector.name != settings.deye.source or settings.owner_id is None \
                or not service.reserve.url:
            raise HTTPException(404, "no TOU table here")
        user = webapp_user(request.headers.get("X-Telegram-Init-Data", ""), settings.bots[collector.config.bot], now)
        if user is None or user["id"] != settings.owner_id:
            raise HTTPException(403, "the owner's only")
        plan = await service.reserve.plan()
        if plan is None:
            raise HTTPException(503, "deye-reserve has no plan")
        return JSONResponse(charge_table(plan, now), headers=NO_CACHE)

    @app.get("/api/v1/inverter/latest")
    async def inverter_latest():
        """The last Deye reading and the grid state, for deye-reserve (same data as the mini app's)."""
        monitor = service.inverter
        if monitor is None:
            raise HTTPException(404, "the inverter is not configured")
        now = now_kyiv()
        grid = monitor.grid
        return JSONResponse({
            "source": monitor.source,
            "reading": monitor.payload(now),
            "last_success": monitor.last_success.isoformat() if monitor.last_success else None,
            "last_error": monitor.last_error or None,
            "grid": {"status": grid.status.name, "since": grid.since.isoformat()} if grid else None,
        }, headers=NO_CACHE)

    @app.get("/metrics")
    async def metrics(request: Request):
        """The inverter's last reading and what is failing, for VictoriaMetrics, on the LAN only: not through the
        Cloudflare tunnel."""
        if request.headers.get("cf-connecting-ip"):
            raise HTTPException(404, "Not Found")
        text = exposition(service.inverter) + problem_exposition(service.failing())
        if service.energy is not None:
            text += service.energy.exposition()
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4")

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
        inverter = None
        if service.inverter is not None:
            m = service.inverter
            inverter = {
                "source": m.source,
                "last_success": m.last_success.isoformat() if m.last_success else None,
                "collected_at": m.latest.at.isoformat() if m.latest else None,
                "failures": m.failures,
                "last_error": m.last_error or None,
                "cadence_s": m.cadence,
                "last_lag_s": m.last_lag_s,
                "calls_per_reading": round(m.calls / m.readings, 2) if m.readings else None,
            }
        problems = service.problems(now)
        ok = not problems
        body = {"status": "ok" if ok else "degraded", "version": APP_VERSION, "problems": problems,
                "sources": sources, "power": power, "inverter": inverter,
                "energy": service.energy.status() if service.energy is not None else None}
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
