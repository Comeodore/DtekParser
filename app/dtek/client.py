"""Talks to dtek-*.com.ua through a real browser page.

The site is behind Incapsula (plain HTTP gets a JavaScript challenge), so a
headless Chromium holds the session. The shutdowns page is loaded once to pass
the challenge and to read the embedded DisconSchedule.fact and the CSRF token.
The tab then parks on the site's robots.txt, so none of the site's scripts keep
running (an idle shutdowns page costs ~15% CPU, a parked tab ~0.2%), and the
getHomeNum request the page makes when an address is typed is sent from there
with fetch(), carrying the same headers jQuery and yii.js would add.

Recovery ladder on consecutive failures: reload the page, then a fresh browser
context, then a relaunched browser.

Cookies of the last successful load are saved per source and seed every new
context, so a reset or restart keeps the Incapsula session instead of showing
up as a brand-new visitor (a burst of those is what earns an hCaptcha). An
hCaptcha cannot be passed headless: tools/solve_captcha.sh opens the page over
VNC, and the cookies it saves are picked up by the next context.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route, async_playwright

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BLOCKED_HOSTS = ("hotjar.", "google-analytics.", "googletagmanager.", "doubleclick.", "facebook.")

# Nobody looks at the page; keep rendering cheap while it is loaded.
VIEWPORT = {"width": 400, "height": 300}
NO_ANIMATIONS_CSS = "*,*::before,*::after{animation:none!important;transition:none!important}"
PARKING_PATH = "/robots.txt"
# DisconSchedule is a top-level `let`, so it is not a property of window.
READY_JS = "() => !!(window.jQuery && typeof DisconSchedule !== 'undefined' && DisconSchedule.fact)"
READ_JS = """() => ({
    fact: DisconSchedule.fact,
    names: (DisconSchedule.preset && DisconSchedule.preset.sch_names) || {},
    timeTypes: Object.keys((DisconSchedule.preset && DisconSchedule.preset.time_type) || {}),
    ajaxUrl: (document.querySelector('meta[name=ajaxUrl]') || {}).content || '/ua/ajax',
    csrfToken: (document.querySelector('meta[name=csrf-token]') || {}).content || '',
})"""
# The request discon-schedule.js sends via $.post: same form encoding as
# jQuery.param, same headers as jQuery (X-Requested-With) and yii.js (X-CSRF-Token).
CALL_JS = """async (a) => {
    const body = new URLSearchParams();
    body.append('method', 'getHomeNum');
    let i = 0;
    const field = (name, value) => {
        body.append(`data[${i}][name]`, name);
        body.append(`data[${i}][value]`, value);
        i++;
    };
    if (a.city) field('city', a.city);
    field('street', a.street);
    field('updateFact', a.updateFact);
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), a.timeout);
    try {
        const response = await fetch(a.url, {
            method: 'POST', body, credentials: 'same-origin', signal: abort.signal,
            headers: {'X-CSRF-Token': a.csrf, 'X-Requested-With': 'XMLHttpRequest',
                      'Accept': 'application/json, text/javascript, */*; q=0.01'},
        });
        const text = await response.text();
        if (!response.ok) throw new Error(`getHomeNum HTTP ${response.status}: ${text.slice(0, 120)}`);
        try { return JSON.parse(text); }
        catch (e) { throw new Error(`getHomeNum returned non-JSON: ${text.slice(0, 120)}`); }
    } finally {
        clearTimeout(timer);
    }
}"""


class FetchError(Exception):
    """The site could not be read this time; the caller just tries again later."""


@dataclass(frozen=True)
class RawData:
    answer: dict
    fact: dict
    names: dict
    time_types: list = field(default_factory=list)


async def _filter_requests(route: Route) -> None:
    request = route.request
    url = request.url
    if "_Incapsula_Resource" in url:
        await route.continue_()
    elif request.resource_type in BLOCKED_RESOURCE_TYPES or any(h in url for h in BLOCKED_HOSTS):
        await route.abort()
    else:
        await route.continue_()


class BrowserPool:
    """One Chromium shared by all sources, relaunched when it dies, is reset or gets old."""

    MAX_AGE_S = 24 * 3600

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._launched_at = 0.0
        self._lock = asyncio.Lock()

    async def new_context(self, storage_state: Optional[str] = None) -> BrowserContext:
        async with self._lock:
            too_old = time.monotonic() - self._launched_at > self.MAX_AGE_S
            if self._browser is None or not self._browser.is_connected() or too_old:
                await self._launch()
            assert self._browser is not None
            context = await self._browser.new_context(
                user_agent=USER_AGENT, locale="uk-UA", timezone_id="Europe/Kyiv", viewport=VIEWPORT,
                storage_state=storage_state,
            )
            await context.route("**/*", _filter_requests)
            return context

    async def _launch(self) -> None:
        await self._close_browser()
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu"],
        )
        self._launched_at = time.monotonic()
        logger.info("Browser launched")

    async def reset(self) -> None:
        async with self._lock:
            await self._close_browser()

    async def _close_browser(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning(f"Error closing browser: {e}")
            self._browser = None

    async def close(self) -> None:
        await self.reset()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


class DtekSite:
    PAGE_TIMEOUT_MS = 45_000
    READY_TIMEOUT_S = 30.0
    CALL_TIMEOUT_MS = 15_000
    # Reload regularly anyway: refreshes the session and the embedded schedule.
    PAGE_MAX_AGE_S = 30 * 60
    CONTEXT_MAX_AGE_S = 6 * 3600
    CONTEXT_RESET_AFTER = 3
    BROWSER_RESET_AFTER = 6

    def __init__(self, pool: BrowserPool, name: str, url: str, street: str, settlement: Optional[str],
                 state_path: Optional[str] = None):
        self._pool = pool
        self._state_path = state_path
        self._name = name
        self._url = url
        self._street = street
        self._settlement = settlement
        self._context: Optional[BrowserContext] = None
        self._context_created = 0.0
        self._page: Optional[Page] = None
        self._loaded_at = 0.0
        self._fact: dict = {}
        self._names: dict = {}
        self._time_types: list = []
        self._ajax_url = "/ua/ajax"
        self._csrf = ""
        self._park_warned = False
        self._failures = 0

    async def fetch(self) -> RawData:
        try:
            await self._drop_stale_context()
            if self._page is None or self._page.is_closed() or time.monotonic() - self._loaded_at > self.PAGE_MAX_AGE_S:
                await self._load()
            assert self._page is not None
            answer = await asyncio.wait_for(
                self._page.evaluate(CALL_JS, {
                    "url": self._ajax_url,
                    "city": self._settlement or "",
                    "street": self._street,
                    "updateFact": str(self._fact.get("update", "")),
                    "csrf": self._csrf,
                    "timeout": self.CALL_TIMEOUT_MS,
                }),
                timeout=self.CALL_TIMEOUT_MS / 1000 + 10,
            )
            if not isinstance(answer, dict) or answer.get("result") is not True:
                raise FetchError(f"getHomeNum answered result={answer.get('result') if isinstance(answer, dict) else answer!r}")
        except Exception as e:
            await self._on_failure(e)
            raise FetchError(f"{type(e).__name__}: {e}") from e

        if self._failures:
            logger.info(f"[{self._name}] site reachable again after {self._failures} failed attempts")
        self._failures = 0
        fresh_fact = answer.get("fact")
        if isinstance(fresh_fact, dict):
            logger.info(f"[{self._name}] DTEK schedule data updated: {self._fact.get('update')} -> {fresh_fact.get('update')}")
            self._fact = fresh_fact
            preset = answer.get("preset")
            if isinstance(preset, dict) and isinstance(preset.get("sch_names"), dict):
                self._names = preset["sch_names"]
        return RawData(answer, self._fact, self._names, self._time_types)

    async def _drop_stale_context(self) -> None:
        """A context whose browser was relaunched (or that is just old) is replaced, not failed on."""
        if self._context is None:
            return
        browser = self._context.browser
        alive = browser is not None and browser.is_connected()
        if not alive or time.monotonic() - self._context_created > self.CONTEXT_MAX_AGE_S:
            await self._close_page()
            await self._close_context()

    async def _load(self) -> None:
        await self._close_page()
        if self._context is None:
            self._context = await self._new_context()
            self._context_created = time.monotonic()
        page = await self._context.new_page()
        try:
            await page.goto(self._url, wait_until="domcontentloaded", timeout=self.PAGE_TIMEOUT_MS)
            await self._wait_ready(page)
            await page.add_style_tag(content=NO_ANIMATIONS_CSS)
            info = await page.evaluate(READ_JS)
            if not info.get("csrfToken"):
                raise FetchError("page has no CSRF token")
            await self._park(page)
        except Exception:
            await page.close()
            raise
        self._page = page
        self._fact = info.get("fact") or {}
        self._names = info.get("names") or {}
        self._time_types = info.get("timeTypes") or []
        self._ajax_url = info.get("ajaxUrl") or "/ua/ajax"
        self._csrf = info["csrfToken"]
        self._loaded_at = time.monotonic()
        logger.info(f"[{self._name}] page loaded, schedule data from {self._fact.get('update')}")
        await self._save_state()

    async def _new_context(self) -> BrowserContext:
        """A context seeded with the cookies of the last successful load, if there are any."""
        if self._state_path and os.path.exists(self._state_path):
            try:
                return await self._pool.new_context(self._state_path)
            except Exception as e:
                logger.warning(f"[{self._name}] saved cookies unusable, starting clean: {e}")
        return await self._pool.new_context()

    async def _save_state(self) -> None:
        if not self._state_path or self._context is None:
            return
        tmp = self._state_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            await self._context.storage_state(path=tmp)
            os.replace(tmp, self._state_path)
        except Exception as e:
            logger.warning(f"[{self._name}] could not save cookies to {self._state_path}: {e}")

    async def _park(self, page: Page) -> None:
        """Move the tab to a static document of the same origin; cookies stay, scripts stop.

        Best effort: the request works from any page of the origin, parking only saves CPU.
        """
        origin = page.url.split("/", 3)
        target = f"{origin[0]}//{origin[2]}{PARKING_PATH}"
        try:
            response = await page.goto(target, wait_until="domcontentloaded", timeout=self.PAGE_TIMEOUT_MS)
            status = response.status if response else None
        except Exception as e:
            status = f"{type(e).__name__}: {e}"
        if status != 200 and not self._park_warned:
            self._park_warned = True
            logger.warning(f"[{self._name}] could not park the tab on {target} ({status}); "
                           f"requests still work, the page just keeps using CPU")

    async def _wait_ready(self, page: Page) -> None:
        deadline = time.monotonic() + self.READY_TIMEOUT_S
        while True:
            remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
            try:
                await page.wait_for_function(READY_JS, timeout=remaining_ms)
                return
            except Exception as e:
                # The Incapsula challenge reloads the page, destroying the context we wait in.
                text = str(e).lower()
                if ("context was destroyed" in text or "navigat" in text) and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                    continue
                if any("hcaptcha.com" in frame.url for frame in page.frames):
                    raise FetchError("Incapsula shows an hCaptcha; solve it with tools/solve_captcha.sh "
                                     f"{self._name}") from e
                raise

    async def _on_failure(self, error: Exception) -> None:
        self._failures += 1
        await self._close_page()
        if self._failures % self.BROWSER_RESET_AFTER == 0:
            logger.warning(f"[{self._name}] {self._failures} failures in a row, relaunching the browser")
            await self._close_context()
            await self._pool.reset()
        elif self._failures >= self.CONTEXT_RESET_AFTER:
            await self._close_context()

    async def _close_page(self) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None

    async def _close_context(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

    async def close(self) -> None:
        await self._close_page()
        await self._close_context()
