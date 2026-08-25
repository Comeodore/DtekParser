import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, Browser, BrowserContext, Playwright, Page
from bs4 import BeautifulSoup

from app.models import CurrentOutage, DaySchedule, HourSlot, OutageStatus, CellStatus

logger = logging.getLogger(__name__)
SCREENSHOTS_DIR = Path("/app/screenshots")
KYIV_TZ = ZoneInfo('Europe/Kyiv')


class DtekParser:
    PAGE_LOAD_TIMEOUT = 60000
    WAIT_AFTER_LOAD = 3000

    def __init__(self, base_url: str, street: str, building: str, settlement: Optional[str] = None):
        self.base_url = base_url
        self.street = street
        self.building = building
        self.settlement = settlement
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._initialized = False
        self._last_restart_date: Optional[date] = None
        self._refresh_page = False

    async def start(self) -> None:
        if self._browser:
            return

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu'
            ]
        )
        self._context = await self._browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        self._page = await self._context.new_page()
        self._last_restart_date = datetime.now(KYIV_TZ).date()
        logger.info("✅ Parser browser started")

    async def stop(self) -> None:
        if self._page:
            await self._page.close()
            self._page = None
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._initialized = False
        logger.info("✅ Parser browser stopped")

    async def fetch_schedule(self) -> tuple[Optional[CurrentOutage], Optional[DaySchedule], Optional[DaySchedule]]:
        try:
            await self._restart_browser_if_needed()
            return await self._fetch_internal()
        except Exception as e:
            logger.error(f"❌ Parse failed: {e}")
            await self._save_screenshot("error")
            self._initialized = False
            return None, None, None

    async def _restart_browser_if_needed(self) -> None:
        if not self._browser:
            return

        now = datetime.now(KYIV_TZ)
        current_date = now.date()
        current_time = now.time()

        if self._last_restart_date != current_date and current_time.hour == 2:
            self._last_restart_date = current_date
            logger.info("🔄 Restarting browser at 02:00")
            await self.stop()
            await self.start()

    async def _save_screenshot(self, prefix: str) -> None:
        if not self._page:
            return
        try:
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            path = SCREENSHOTS_DIR / f"{prefix}_{timestamp}.png"
            await self._page.screenshot(path=str(path), full_page=True)
            logger.info(f"📸 Screenshot saved: {path}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to save screenshot: {e}")

    async def _fetch_internal(self) -> tuple[Optional[CurrentOutage], Optional[DaySchedule], Optional[DaySchedule]]:
        if not self._page:
            await self.start()

        if not self._initialized:
            await self._initialize_page()
        else:
            await self._refresh_data()

        return await self._extract_data()

    async def _initialize_page(self) -> None:
        logger.info(f"🌐 Loading {self.base_url}")
        await self._page.goto(self.base_url, wait_until='networkidle', timeout=self.PAGE_LOAD_TIMEOUT)
        await self._page.wait_for_timeout(self.WAIT_AFTER_LOAD)
        logger.info(f"📍 Page loaded: {self._page.url}")

        # logger.info("⏳ Waiting 60s for manual captcha solving via VNC...")
        # await self._page.wait_for_timeout(60000)
        # logger.info("✅ Continuing after captcha delay")

        await self._close_modal_if_present()
        await self._fill_settlement()
        await self._fill_street()
        await self._fill_building()

        self._initialized = True
        address = f"{self.settlement}, " if self.settlement else ""
        logger.info(f"✅ Page initialized for {address}{self.street}, {self.building}")

    async def _fill_settlement(self) -> None:
        """Fill settlement field and select from dropdown if settlement is provided."""
        if not self.settlement:
            return
        
        settlement_input = self._page.locator('input[placeholder="Почніть вводити дані українською"]').first
        await settlement_input.fill(self.settlement)
        await self._page.wait_for_timeout(1500)
        
        # Wait for dropdown and click on the first option
        try:
            dropdown_option = self._page.locator('#cityautocomplete-list.autocomplete-items > div').first
            await dropdown_option.wait_for(state='visible', timeout=5000)
            await dropdown_option.click()
            await self._page.wait_for_timeout(1000)
        except Exception as e:
            logger.info(f"⚠️ Dropdown not found, will retry in 60s: {e}")
            raise Exception("Settlement dropdown not found, retry needed")

    async def _fill_street(self) -> None:
        """Fill street field and wait for it to be enabled if settlement was filled."""
        if self.settlement:
            street_input = self._page.locator('input[placeholder="Почніть вводити дані українською"]').nth(1)
            await street_input.wait_for(state='visible', timeout=10000)
            for _ in range(20):
                if await street_input.is_enabled():
                    break
                await self._page.wait_for_timeout(500)
            else:
                logger.error("❌ Street input still disabled after waiting")
        else:
            street_input = self._page.locator('input[placeholder="Почніть вводити дані українською"]').first
        
        await street_input.fill(self.street)
        await self._page.wait_for_timeout(1000)
        await self._page.keyboard.press('Enter')
        await self._page.wait_for_timeout(2000)

    async def _fill_building(self, clear_first: bool = False) -> None:
        """Fill building field."""
        building_input = self._page.locator('input[placeholder="Номер будинку"]')
        await building_input.wait_for(state='visible', timeout=10000)

        for _ in range(10):
            if await building_input.is_enabled():
                break
            await self._page.wait_for_timeout(500)
        else:
            logger.error("❌ Building input still disabled after waiting")

        if clear_first:
            await building_input.clear()
            await self._page.wait_for_timeout(300)
        
        await building_input.fill(self.building)
        await self._page.wait_for_timeout(500 if not clear_first else 300)
        await self._page.keyboard.press('Enter')
        await self._page.wait_for_timeout(3000)

    async def _close_modal_if_present(self) -> None:
        selectors = [
            '.modal.is-open button.modal__close',
            '.modal.is-open button[data-micromodal-close]',
            'button.modal_close',
            'button.modal__close',
            'button.m-attention_close'
        ]
        
        for selector in selectors:
            try:
                close_button = self._page.locator(selector).first
                if await close_button.count() > 0:
                    await close_button.wait_for(state='visible', timeout=3000)
                    await close_button.click(timeout=3000)
                    await self._page.wait_for_timeout(500)
                    logger.info(f"✅ Modal closed using selector: {selector}")
                    return
            except Exception:
                continue

    async def _refresh_data(self) -> None:
        await self._close_modal_if_present()

        if self._refresh_page:
            logger.info("🔄 Reloading page and re-entering data...")
            await self._page.reload(wait_until='networkidle', timeout=self.PAGE_LOAD_TIMEOUT)
            await self._page.wait_for_timeout(self.WAIT_AFTER_LOAD)
            await self._close_modal_if_present()
            await self._fill_settlement()
            await self._fill_street()
            self._refresh_page = False

        await self._fill_building(clear_first=True)

    async def _extract_data(self) -> tuple[Optional[CurrentOutage], Optional[DaySchedule], Optional[DaySchedule]]:
        current_outage = None
        outage_div = self._page.locator('#showCurOutage')
        if await outage_div.count() > 0:
            outage_html = await outage_div.inner_html()
            is_active = 'active' in (await outage_div.get_attribute('class') or '')
            current_outage = self._parse_current_outage(outage_html, is_active)

        schedule_section = await self._page.locator('#discon-fact').count()
        if schedule_section == 0:
            logger.warning("⚠️ Schedule section #discon-fact not found")
            return current_outage, None, None

        schedule_html = await self._page.locator('#discon-fact').inner_html()
        today, tomorrow = self._parse_schedules(schedule_html)

        return current_outage, today, tomorrow

    def _parse_current_outage(self, html: str, is_active: bool) -> CurrentOutage:
        soup = BeautifulSoup(html, 'html.parser')
        
        last_updated = ''
        update_info_span = soup.find('span', class_='_update_info')
        if update_info_span:
            for sibling in update_info_span.next_siblings:
                text = str(sibling).strip()
                if text:
                    last_updated = text[2:].strip()
                    break

        if not is_active:
            return CurrentOutage(status=OutageStatus.POWER_ON, last_updated=last_updated)

        text = soup.get_text(' ', strip=True)

        if 'відсутня електроенергія' not in text.lower():
            return CurrentOutage(status=OutageStatus.POWER_ON, last_updated=last_updated)

        is_emergency = 'Екстренн' in text or 'Аварійн' in text

        reason = ''
        start_time = ''
        restoration_time = ''

        strongs = soup.find_all('strong')
        for strong in strongs:
            strong_text = strong.get_text(strip=True)
            prev_text = ''
            if strong.previous_sibling:
                prev_text = str(strong.previous_sibling).strip().lower()

            if 'причина' in prev_text:
                reason = strong_text
            elif 'початку' in prev_text:
                start_time = strong_text
            elif 'відновлення' in prev_text or 'до ' in strong_text.lower():
                restoration_time = strong_text.removeprefix('до ').strip()

        return CurrentOutage(
            status=OutageStatus.EMERGENCY if is_emergency else OutageStatus.SCHEDULED,
            reason=reason,
            start_time=start_time,
            restoration_time=restoration_time,
            last_updated=last_updated
        )

    def _parse_schedules(self, html: str) -> tuple[Optional[DaySchedule], Optional[DaySchedule]]:
        soup = BeautifulSoup(html, 'html.parser')

        last_updated = ""
        info_span = soup.find('span', class_='update')
        if info_span:
            last_updated = info_span.get_text(strip=True)

        tables = soup.find_all('div', class_='discon-fact-table')
        logger.debug(f"Found {len(tables)} schedule tables")

        now = datetime.now(KYIV_TZ)
        current_date = now.date()

        today_schedule = None
        tomorrow_schedule = None
        all_schedules = []

        for i, table_div in enumerate(tables):
            is_active = 'active' in table_div.get('class', [])
            classes = table_div.get('class', [])
            rel_value = table_div.get('rel', 'no-rel')
            schedule = self._parse_table(table_div, last_updated)

            if schedule:
                schedule_date_obj = self._parse_date(schedule.date)
                if schedule_date_obj:
                    schedule_date = schedule_date_obj.date()
                    if schedule_date == current_date:
                        today_schedule = schedule
                    elif schedule_date == current_date + timedelta(days=1):
                        tomorrow_schedule = schedule
                    else:
                        all_schedules.append(schedule)
                elif is_active:
                    today_schedule = schedule
                else:
                    all_schedules.append(schedule)
            else:
                logger.info(f"Table {i} could not be parsed: is_active={is_active}, rel={rel_value}, classes={classes}")

        if not today_schedule and all_schedules:
            for schedule in all_schedules:
                schedule_date_obj = self._parse_date(schedule.date)
                if schedule_date_obj and schedule_date_obj.date() == current_date:
                    today_schedule = schedule
                    break

        if not tomorrow_schedule and all_schedules:
            for schedule in all_schedules:
                schedule_date_obj = self._parse_date(schedule.date)
                if schedule_date_obj and schedule_date_obj.date() == current_date + timedelta(days=1):
                    tomorrow_schedule = schedule
                    break

        if today_schedule and not tomorrow_schedule:
            logger.info(f"⚠️ Tomorrow schedule not found. Today date: {today_schedule.date}, tables found: {len(tables)}, non-active schedules: {len(all_schedules)}")
            self._refresh_page = True

        return today_schedule, tomorrow_schedule

    def _parse_date(self, date_str: str) -> Optional[datetime]:
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str, "%d.%m.%y")
        except (ValueError, AttributeError):
            return None

    def _parse_table(self, table_div, last_updated: str) -> Optional[DaySchedule]:
        date_str = ""
        date_div = table_div.find_previous('div', class_='dates')
        if date_div:
            rel_value = table_div.get('rel')
            if rel_value:
                matching_date = date_div.find('div', {'rel': rel_value})
                if matching_date:
                    date_span = matching_date.find('span', {'rel': 'date'})
                    if date_span:
                        date_str = date_span.get_text(strip=True)

        table = table_div.find('table')
        if not table:
            if date_str:
                logger.info(f"Table not found but date exists: {date_str}, creating empty schedule")
                return DaySchedule(date=date_str, slots=[], last_updated=last_updated)
            return None

        rows = table.find_all('tr')
        if len(rows) < 2:
            if date_str:
                logger.info(f"Table has less than 2 rows but date exists: {date_str}, creating empty schedule")
                return DaySchedule(date=date_str, slots=[], last_updated=last_updated)
            return None

        header_row = rows[0]
        data_row = rows[1]

        headers = header_row.find_all('th')
        cells = data_row.find_all('td')

        slots = []

        header_divs = [th.find('div') for th in headers[1:] if th.find('div')]
        data_cells = cells[1:]

        for header_div, cell in zip(header_divs, data_cells):
            hour_text = header_div.get_text(strip=True)
            try:
                hour = int(hour_text.split('-')[0])
            except (ValueError, IndexError):
                continue

            status = self._parse_cell_status(cell)
            slots.append(HourSlot(hour=hour, status=status))

        if not date_str and not slots:
            logger.debug("Table has no date and no slots, skipping")
            return None

        return DaySchedule(date=date_str, slots=slots, last_updated=last_updated)

    def _parse_cell_status(self, cell) -> CellStatus:
        cell_classes = cell.get('class', [])

        if 'cell-scheduled' in cell_classes:
            return CellStatus.POWER_OFF
        elif 'cell-first-half' in cell_classes:
            return CellStatus.POWER_OFF_FIRST_30
        elif 'cell-second-half' in cell_classes:
            return CellStatus.POWER_OFF_SECOND_30

        return CellStatus.POWER_ON

    @property
    def is_running(self) -> bool:
        return self._browser is not None