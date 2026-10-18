"""Deye Cloud OpenAPI calls deye-reserve makes itself: load history, and the TOU table (read, write, confirm).

The live reading (SOC, grid) is not fetched here: dtek-service already polls
it and serves it on /api/v1/inverter/latest. The app ID is dtek-service's,
and logging in again does not revoke its token (checked 17.10), so this
service keeps a token of its own, on disk: it lives 60 days.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

import aiohttp

from deye_reserve.planner import Slot, Table

logger = logging.getLogger(__name__)

ORDER_DONE = 666
ORDER_FINAL = (400, 500, ORDER_DONE)
ORDER_STATUS = {0: "очікує відправки", 100: "відправляється", 300: "оновлюється", 400: "перервана",
                500: "помилка", ORDER_DONE: "виконана"}
ORDER_ERRORS = {
    "501": "конфлікт команд: попередня ще виконується",
    "502": "команда не підходить до типу пристрою",
    "505": "системна помилка Deye Cloud",
    "510": "помилка розбору команди",
    "511": "непідтримувана версія прошивки",
    "530": "інвертор не виконав команду",
    "540": "інвертор не відповів вчасно",
}


class DeyeError(Exception):
    pass


class DeyeRateLimited(DeyeError):
    pass


def tou_items(table: Sequence[Slot], power_w: int) -> list[dict]:
    """The six slots as /order/sys/tou/update takes them."""
    return [{"time": slot.time, "soc": slot.soc, "power": power_w, "enableGridCharge": slot.grid_charge,
             "enableGeneration": False} for slot in table]


def tou_request(device_sn: str, table: Sequence[Slot], power_w: int, timeout_s: int = 30) -> dict:
    if len(table) != 6 or table[0].start != 0 or [s.start for s in table] != sorted({s.start for s in table}):
        raise ValueError(f"a TOU table is six slots from 00:00 in increasing time: {table}")
    if any(not 10 <= s.soc <= 100 for s in table):
        raise ValueError(f"TOU SOC out of 10-100: {table}")
    return {"deviceSn": device_sn, "timeUseSettingItems": tou_items(table, power_w), "timeoutSeconds": timeout_s}


def parse_tou(items: Sequence[dict]) -> tuple[Table, list[dict]]:
    """/config/tou answers times as "0700"; returns the table and the items normalised for comparison."""
    table, normal = [], []
    for item in items:
        digits = str(item.get("time", "")).replace(":", "").zfill(4)
        start = int(digits[:2]) * 60 + int(digits[2:])
        soc = int(item.get("soc") or 0)
        gc = bool(item.get("enableGridCharge"))
        table.append(Slot(start, soc, gc))
        normal.append({"time": f"{digits[:2]}:{digits[2:]}", "soc": soc, "power": item.get("power"),
                       "enableGridCharge": gc, "enableGeneration": bool(item.get("enableGeneration"))})
    return tuple(table), normal


class DeyeClient:
    TOKEN_MARGIN_S = 24 * 3600

    def __init__(self, base_url: str, app_id: str, app_secret: str, email: str, password_sha256: str,
                 session: aiohttp.ClientSession, token_path: Path):
        self._base = base_url.rstrip("/") + "/v1.0"
        self._app_id = app_id
        self._secret = app_secret
        self._email = email
        self._password = password_sha256
        self._session = session
        self._token_path = token_path
        self._token: Optional[str] = None
        self._token_until = 0.0
        self.calls = 0
        self._load_token()

    def _load_token(self) -> None:
        try:
            data = json.loads(self._token_path.read_text())
            self._token, self._token_until = data["token"], float(data["until"])
        except (OSError, ValueError, KeyError):
            pass

    def _save_token(self) -> None:
        tmp = self._token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"token": self._token, "until": self._token_until}))
        os.chmod(tmp, 0o600)
        tmp.replace(self._token_path)

    async def _request(self, method: str, path: str, body: Optional[dict] = None, token: Optional[str] = None) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.calls += 1
        async with self._session.request(method, f"{self._base}/{path}", json=body, headers=headers) as r:
            if r.status == 429:
                raise DeyeRateLimited(f"{path}: rate limited")
            if r.status >= 400:
                raise DeyeError(f"{path}: HTTP {r.status}")
            data = await r.json(content_type=None)
        if not data.get("success"):
            raise DeyeError(f"{path}: {data.get('code')} {data.get('msg')}")
        return data

    async def _auth(self) -> str:
        if self._token and time.time() < self._token_until:
            return self._token
        data = await self._request("POST", f"account/token?appId={self._app_id}",
                                   {"appSecret": self._secret, "email": self._email, "password": self._password})
        self._token = data["accessToken"]
        self._token_until = time.time() + max(0.0, float(data.get("expiresIn") or 0) - self.TOKEN_MARGIN_S)
        try:
            self._save_token()
        except OSError as e:
            logger.warning(f"Deye token not saved: {e}")
        logger.info("Deye Cloud token obtained")
        return self._token

    async def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        cached = bool(self._token) and time.time() < self._token_until
        try:
            return await self._request(method, path, body, await self._auth())
        except DeyeRateLimited:
            raise
        except DeyeError:
            if not cached:
                raise
            # A revoked token fails like anything else; one retry with a fresh one.
            self._token, self._token_until = None, 0.0
            return await self._request(method, path, body, await self._auth())

    async def inverter_sn(self, station_id: int) -> str:
        data = await self._call("POST", "station/device", {"stationIds": [station_id], "page": 1, "size": 20})
        inverters = [d for d in data.get("deviceListItems") or [] if d.get("deviceType") == "INVERTER"]
        if not inverters:
            raise DeyeError(f"station {station_id} has no inverter")
        return str(inverters[0]["deviceSn"])

    async def history_power(self, station_id: int, start: datetime, end: datetime) -> list[dict]:
        """Station power every 5 minutes: consumptionPower, generationPower, batterySOC, ..."""
        data = await self._call("POST", "station/history/power", {
            "stationId": station_id, "startTimestamp": int(start.timestamp()), "endTimestamp": int(end.timestamp())})
        return data.get("stationDataItems") or []

    async def read_tou(self, device_sn: str) -> tuple[Table, list[dict]]:
        data = await self._call("POST", "config/tou", {"deviceSn": device_sn})
        return parse_tou(data.get("timeUseSettingItems") or [])

    async def update_tou(self, request: dict) -> str:
        data = await self._call("POST", "order/sys/tou/update", request)
        order_id = data.get("orderId")
        if order_id is None:
            raise DeyeError(f"TOU update accepted without an orderId: {data}")
        return str(order_id)

    async def order(self, order_id: str) -> dict:
        return await self._call("GET", f"order/{order_id}")
