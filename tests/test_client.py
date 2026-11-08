"""Telling DTEK's own trouble (waited out) from ours (alarmed on), without a browser."""
import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.dtek.client import DtekSite, FetchError, SiteDown, site_down


@pytest.mark.parametrize("error, down", [
    (TimeoutError(), True),
    (PlaywrightTimeoutError("Page.goto: Timeout 45000ms exceeded."), True),
    (Exception("Page.evaluate: Error: getHomeNum timed out after 15000 ms"), True),
    (Exception("Page.evaluate: AbortError: signal is aborted without reason"), True),
    (Exception("Page.evaluate: Error: getHomeNum HTTP 502: <html>"), True),
    (Exception("Page.goto: net::ERR_CONNECTION_RESET at https://www.dtek-krem.com.ua/ua/shutdowns"), True),
    (Exception("Page.evaluate: TypeError: Failed to fetch"), True),
    (Exception("getHomeNum returned non-JSON: <html>Сайт перевантажений, спробуйте пізніше"), True),
    (SiteDown("page still loading after 30 s"), True),
    (Exception("Page.evaluate: Error: getHomeNum HTTP 403: Request unsuccessful. Incapsula incident ID"), False),
    (Exception("Page.goto: net::ERR_NAME_NOT_RESOLVED"), False),
    (FetchError("Incapsula shows an hCaptcha; solve it with tools/solve_captcha.sh krem"), False),
    (FetchError("getHomeNum answered result=False"), False),
    (FetchError("page has no CSRF token"), False),
])
def test_site_down(error, down):
    assert site_down(error) is down


class FakePage:
    def __init__(self, state=None, fail=False):
        self.state, self.fail = state, fail

    async def evaluate(self, script):
        if self.fail:
            raise Exception("Target page, context or browser has been closed")
        return self.state


def page(state="complete", title="", text=""):
    return FakePage({"state": state, "title": title, "text": text})


@pytest.fixture
def site():
    return DtekSite(None, "krem", "https://www.dtek-krem.com.ua/ua/shutdowns", "вул. Садова", "с. Круглик")


@pytest.mark.parametrize("shown, status", [
    (page(title="502 Bad Gateway", text="502 Bad Gateway nginx"), 200),
    (page(text="anything"), 503),
    (page(text="Через значне навантаження сайт тимчасово недоступний. Спробуйте пізніше."), 200),
    (page(text="На сайті ведуться технічні роботи"), 200),
    (page(state="interactive", title="Відсутня електроенергія?", text="Перевірка поточних відключень"), 200),
    (page(), 200),
    (page(title="ДТЕК", text="Сайт перевантажений. " + "Меню " * 300), 200),
    (FakePage(fail=True), 200),
])
async def test_a_page_without_schedule_is_the_site_down(site, shown, status):
    assert isinstance(await site._no_schedule(shown, status), SiteDown)


async def test_a_finished_page_we_do_not_recognise_is_ours(site):
    error = await site._no_schedule(page(title="Відсутня електроенергія?", text="Перевірка поточних відключень"), 200)
    assert type(error) is FetchError
    assert str(error).startswith("page has no schedule data (status 200, complete, 'Відсутня електроенергія?'")


async def test_load_words_on_the_full_schedule_page_are_not_an_overload(site):
    text = "Дякуємо, це допоможе зменшити навантаження на мережу. " + "Перевірка поточних відключень " * 50
    assert type(await site._no_schedule(page(title="Відсутня електроенергія?", text=text), 200)) is FetchError


async def test_an_incapsula_block_page_is_ours(site):
    shown = page(text="Request unsuccessful. Incapsula incident ID: 1234-5678")
    assert type(await site._no_schedule(shown, 200)) is FetchError
