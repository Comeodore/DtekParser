import asyncio
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import get_settings, PARSER_CONFIGS, PARSE_INTERVAL_SECONDS
from app.database import Database
from app.heartbeat import beat
from app.parser import DtekParser

logger = logging.getLogger(__name__)
KYIV_TZ = ZoneInfo('Europe/Kyiv')


class ScheduleService:
    def __init__(self):
        settings = get_settings()
        self.parser_type = settings.parser_type.lower()
        config = PARSER_CONFIGS[self.parser_type]
        
        settlement = settings.settlement or None

        self.parser = DtekParser(
            config["url"],
            settings.street,
            settings.building,
            settlement
        )
        self.schedule_id = config["schedule_id"]
        self.address = f"{settlement}, {settings.street}, {settings.building}" if settlement else f"{settings.street}, {settings.building}"
        
        self.db = Database(settings.database_url)
        self.interval = PARSE_INTERVAL_SECONDS
        self._shutdown = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._last_parse: Optional[datetime] = None
        
        self._prev_outage_status: Optional[str] = None
        self._prev_today_slots: Optional[list] = None
        self._prev_tomorrow_slots: Optional[list] = None
        self._prev_today_date: Optional[str] = None

    async def start(self) -> None:
        await self.db.connect()
        await self._load_previous_state()
        await self.parser.start()
        self._task = asyncio.create_task(self._parse_loop())
        logger.info(f"✅ Schedule service started (interval: {self.interval}s)")

    async def _load_previous_state(self) -> None:
        saved = await self.db.get_schedule(schedule_id=self.schedule_id)
        if saved:
            current = self.db.parse_current_outage(saved.get('current_outage'))
            today = self.db.parse_day_schedule(saved.get('today'))
            tomorrow = self.db.parse_day_schedule(saved.get('tomorrow'))

            self._prev_outage_status = current.status.value if current else None
            self._prev_today_slots = [(s.hour, s.status.value) for s in today.slots] if today else None
            self._prev_tomorrow_slots = [(s.hour, s.status.value) for s in tomorrow.slots] if tomorrow else None
            self._prev_today_date = today.date if today else None

        logger.info("📂 Loaded previous state from DB")

    async def stop(self) -> None:
        self._shutdown.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.parser.stop()
        await self.db.close()
        logger.info("✅ Schedule service stopped")

    async def _parse_loop(self) -> None:
        await asyncio.sleep(5)

        while not self._shutdown.is_set():
            try:
                await self._parse_and_save()
            except Exception as e:
                logger.error(f"❌ Parse error: {e}")

            # Functional heartbeat: proves the parse loop is still cycling.
            # id is derived from PARSER_TYPE so kem/krem report separately.
            beat("parse loop alive", service_id=f"dtek-parser-{self.parser_type}")

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def _parse_and_save(self) -> None:
        try:
            current, today, tomorrow = await self.parser.fetch_schedule()
        except Exception as e:
            logger.error(f"❌ Parse error: {e}")
            return
        
        if not today and not current:
            # logger.warning("⚠️ Failed to fetch schedule and current outage")
            return
        
        if not today:
            # logger.warning("⚠️ Failed to fetch schedule, saving current_outage only")
            await self.db.save_current_outage_only(current, schedule_id=self.schedule_id)
            self._last_parse = datetime.now(KYIV_TZ)
            return
            
        self._prev_outage_status, self._prev_today_slots, self._prev_tomorrow_slots, self._prev_today_date = \
            self._detect_and_log_changes(current, today, tomorrow)
        await self.db.save_schedule(current, today, tomorrow, schedule_id=self.schedule_id)
        self._last_parse = datetime.now(KYIV_TZ)

    def _detect_and_log_changes(self, current, today, tomorrow) -> tuple[Optional[str], Optional[list], Optional[list], Optional[str]]:
        outage_status = current.status.value if current else None
        today_slots = [(s.hour, s.status.value) for s in today.slots] if today else None
        tomorrow_slots = [(s.hour, s.status.value) for s in tomorrow.slots] if tomorrow else None

        today_date = today.date if today else 'none'
        tomorrow_date = tomorrow.date if tomorrow else 'none'

        if self._prev_outage_status is None:
            logger.info(f"📊 Initial: outage={outage_status}, today={today_date}")
            return outage_status, today_slots, tomorrow_slots, today_date

        if outage_status != self._prev_outage_status:
            logger.info(f"⚡ Outage status changed: {self._prev_outage_status} → {outage_status}")

        if today_date != self._prev_today_date and self._prev_today_date is not None:
            logger.info(f"🌅 New day detected: {self._prev_today_date} → {today_date}")
            return outage_status, today_slots, tomorrow_slots, today_date

        if today_slots != self._prev_today_slots:
            logger.info(f"📅 Today ({today_date}) schedule changed")

        if tomorrow_slots != self._prev_tomorrow_slots:
            if self._prev_tomorrow_slots is None and tomorrow_slots is not None:
                logger.info(f"📅 Tomorrow schedule published ({tomorrow_date})")
            elif self._prev_tomorrow_slots is not None and tomorrow_slots is None:
                logger.info("📅 Tomorrow schedule disappeared")
            else:
                logger.info(f"📅 Tomorrow ({tomorrow_date}) schedule changed")

        return outage_status, today_slots, tomorrow_slots, today_date

    async def get_current_schedule(self) -> Optional[dict]:
        return await self.db.get_schedule(self.schedule_id)

    @property
    def last_parse(self) -> Optional[datetime]:
        return self._last_parse

    @property
    def is_running(self) -> bool:
        return self.parser.is_running and self.db.is_connected


schedule_service = ScheduleService()
