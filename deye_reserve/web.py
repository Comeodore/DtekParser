"""Local HTTP: webhooks from dtek-service in; health, the current plan and today's sun out."""
from __future__ import annotations

from aiohttp import web

from deye_reserve.service import ReserveService

EVENTS = ("schedule", "grid")


def make_app(service: ReserveService) -> web.Application:
    async def event(request: web.Request) -> web.Response:
        kind = request.match_info["kind"]
        if kind not in EVENTS:
            raise web.HTTPNotFound(text=f"unknown event {kind!r}")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        due = service.on_event(f"{kind}" + (f" ({detail})" if detail else ""))
        return web.json_response({"queued": kind, "replan_at": due.isoformat()}, status=202)

    async def health(_: web.Request) -> web.Response:
        body = service.health()
        return web.json_response(body, status=200 if body["status"] == "ok" else 503)

    async def plan(_: web.Request) -> web.Response:
        body = service.plan_payload()
        if body is None:
            raise web.HTTPNotFound(text="no plan yet")
        return web.json_response(body)

    async def sun(_: web.Request) -> web.Response:
        body = service.sun_today()
        if body is None:
            raise web.HTTPNotFound(text="no forecast for today")
        return web.json_response(body)

    app = web.Application()
    app.router.add_post("/events/{kind}", event)
    app.router.add_get("/sun", sun)
    app.router.add_get("/health", health)
    app.router.add_get("/plan", plan)
    return app
