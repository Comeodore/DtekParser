"""HTTP edges: dtek-service's local API, Open-Meteo, Telegram."""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Optional

import aiohttp

from deye_reserve.config import Config

logger = logging.getLogger(__name__)

MODELS = ("ecmwf_ifs025", "ukmo_seamless", "gem_seamless", "icon_seamless", "gfs_seamless",
          "meteofrance_seamless", "cma_grapes_global")


class HttpSources:
    def __init__(self, config: Config, session: aiohttp.ClientSession):
        self._cfg = config
        self._session = session

    async def _get(self, url: str, params: Optional[dict] = None) -> dict:
        async with self._session.get(url, params=params) as r:
            if r.status >= 400:
                raise RuntimeError(f"{url}: HTTP {r.status}")
            return await r.json(content_type=None)

    async def schedule(self) -> dict:
        return await self._get(f"{self._cfg.dtek_url}/api/v1/schedule")

    async def inverter(self) -> dict:
        return await self._get(f"{self._cfg.dtek_url}/api/v1/inverter/latest")

    async def previous_runs(self, first: date, last: date) -> dict:
        """Each model's forecast for those days as it was a day earlier."""
        return await self._get("https://previous-runs-api.open-meteo.com/v1/forecast", {
            "latitude": self._cfg.latitude, "longitude": self._cfg.longitude,
            "hourly": "shortwave_radiation_previous_day1", "models": ",".join(MODELS), "timezone": "Europe/Kyiv",
            "start_date": first.isoformat(), "end_date": last.isoformat()})

    async def weather(self) -> tuple[dict, Optional[dict]]:
        common = {"latitude": self._cfg.latitude, "longitude": self._cfg.longitude, "hourly": "shortwave_radiation",
                  "timezone": "Europe/Kyiv", "forecast_days": 3}
        multi = await self._get("https://api.open-meteo.com/v1/forecast", {**common, "models": ",".join(MODELS)})
        try:
            ensemble = await self._get("https://ensemble-api.open-meteo.com/v1/ensemble",
                                       {**common, "models": "ecmwf_ifs025"})
        except Exception as e:
            # Without the ensemble the pessimistic case is half the median: worse, not wrong.
            logger.warning(f"Open-Meteo ensemble failed: {type(e).__name__}: {e}")
            ensemble = None
        return multi, ensemble


class SolcastLimited(Exception):
    pass


class Solcast:
    """The rooftop site's forecast; the key goes in a header, never in a URL that could reach a log."""

    def __init__(self, api_key: str, site_id: str, session: aiohttp.ClientSession):
        self._url = f"https://api.solcast.com.au/rooftop_sites/{site_id}/forecasts"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._session = session

    async def forecasts(self) -> dict:
        async with self._session.get(self._url, params={"format": "json", "hours": 72}, headers=self._headers) as r:
            if r.status == 429:
                raise SolcastLimited("Solcast: 429")
            if r.status >= 400:
                raise RuntimeError(f"Solcast: HTTP {r.status}")
            return await r.json(content_type=None)


class Telegram:
    """sendMessage to the owner's chat through the krem bot; a few retries, then the message is dropped."""
    TRIES = 4

    def __init__(self, token: str, chat_id: str, session: aiohttp.ClientSession, dry_run: bool = False):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat = chat_id
        self._session = session
        self._dry_run = dry_run

    async def send(self, text: str) -> bool:
        if self._dry_run:
            logger.info(f"[dry-run] -> {self._chat}:\n{text}")
            return True
        delay = 5.0
        for attempt in range(1, self.TRIES + 1):
            try:
                async with self._session.post(self._url, json={
                        "chat_id": self._chat, "text": text, "parse_mode": "HTML",
                        "disable_web_page_preview": True}) as r:
                    data = await r.json(content_type=None)
                if data.get("ok"):
                    return True
                retry = (data.get("parameters") or {}).get("retry_after")
                if r.status == 429 and retry:
                    delay = float(retry)
                elif 400 <= r.status < 500:
                    logger.error(f"Telegram refused the message: {r.status} {data.get('description')}")
                    return False
                error = f"{r.status} {data.get('description')}"
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                error = f"{type(e).__name__}: {e}"
            logger.warning(f"Telegram send failed ({attempt}/{self.TRIES}): {error}")
            if attempt < self.TRIES:
                await asyncio.sleep(delay)
                delay = min(delay * 3, 300.0)
        return False
