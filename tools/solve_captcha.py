"""Runs inside tools/solve_captcha.sh: waits for a human to pass the hCaptcha, then saves the cookies."""
import asyncio
import os
import sys
import time

from playwright.async_api import async_playwright

from app.dtek.client import READY_JS, USER_AGENT

WAIT_S = 20 * 60


async def main(source: str) -> int:
    url = f"https://www.dtek-{source}.com.ua/ua/shutdowns"
    state_path = f"/app/data/state-{source}.json"
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage",
                  "--window-position=0,0", "--window-size=1280,900"],
        )
        context = await browser.new_context(user_agent=USER_AGENT, locale="uk-UA", timezone_id="Europe/Kyiv",
                                            no_viewport=True)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        print(f"{url} is open on VNC; waiting up to {WAIT_S // 60} min for the CAPTCHA", flush=True)
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            try:
                if await page.evaluate(READY_JS):
                    break
            except Exception:
                pass  # the challenge reloads the page
            await asyncio.sleep(2)
        else:
            print("Timed out, nothing saved", flush=True)
            return 1
        await context.storage_state(path=state_path + ".tmp")
        os.replace(state_path + ".tmp", state_path)
        print(f"Passed; cookies saved to data/state-{source}.json", flush=True)
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
