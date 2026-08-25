import json
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import asyncpg

from app.models import CurrentOutage, DaySchedule, HourSlot, OutageStatus, CellStatus

logger = logging.getLogger(__name__)
KYIV_TZ = ZoneInfo('Europe/Kyiv')


class Database:
    def __init__(self, url: str):
        self.url = url
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        try:
            self.pool = await asyncpg.create_pool(
                self.url,
                min_size=2,
                max_size=10,
                command_timeout=10,
                server_settings={'application_name': 'DtekParserAPI'}
            )
            async with self.pool.acquire() as conn:
                await conn.execute('SELECT 1')
            logger.info("✅ Database connected")
        except Exception as e:
            logger.error(f"❌ Database connection failed: {e}")
            raise

    async def get_schedule(self, schedule_id: int = 1) -> Optional[dict]:
        if not self.pool:
            return None

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT current_outage, today_schedule, tomorrow_schedule, updated_at FROM dtek_schedule WHERE id = $1",
                    schedule_id
                )
                if row:
                    return {
                        'current_outage': self._parse_json(row['current_outage']),
                        'today': self._parse_json(row['today_schedule']),
                        'tomorrow': self._parse_json(row['tomorrow_schedule']),
                        'updated_at': row['updated_at']
                    }
                return None
        except Exception as e:
            logger.error(f"❌ Failed to get schedule: {e}")
            return None

    def _parse_json(self, value) -> Optional[dict]:
        if value is None:
            return None
        if isinstance(value, str):
            return json.loads(value) if value else None
        return value

    async def save_schedule(
        self,
        current_outage: Optional[CurrentOutage],
        today: Optional[DaySchedule],
        tomorrow: Optional[DaySchedule],
        schedule_id: int = 1
    ) -> None:
        if not self.pool:
            return

        try:
            current_json = self._outage_to_json(current_outage)
            today_json = self._schedule_to_json(today)
            tomorrow_json = self._schedule_to_json(tomorrow)
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE dtek_schedule
                    SET current_outage = $1,
                        today_schedule = $2,
                        tomorrow_schedule = $3,
                        updated_at = $4
                    WHERE id = $5
                    """,
                    current_json, today_json, tomorrow_json, now, schedule_id
                )
        except Exception as e:
            logger.error(f"❌ Failed to save schedule: {e}")

    async def save_current_outage_only(
        self,
        current_outage: Optional[CurrentOutage],
        schedule_id: int = 1
    ) -> None:
        if not self.pool:
            return

        try:
            current_json = self._outage_to_json(current_outage)
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE dtek_schedule
                    SET current_outage = $1,
                        updated_at = $2
                    WHERE id = $3
                    """,
                    current_json, now, schedule_id
                )
        except Exception as e:
            logger.error(f"❌ Failed to save current outage: {e}")

    def _outage_to_json(self, outage: Optional[CurrentOutage]) -> Optional[str]:
        if not outage:
            return None
        return json.dumps({
            'status': outage.status.value,
            'reason': outage.reason,
            'start_time': outage.start_time,
            'restoration_time': outage.restoration_time,
            'last_updated': outage.last_updated
        })

    def _schedule_to_json(self, schedule: Optional[DaySchedule]) -> Optional[str]:
        if not schedule:
            return None
        return json.dumps({
            'date': schedule.date,
            'slots': [{'hour': s.hour, 'status': s.status.value} for s in schedule.slots],
            'last_updated': schedule.last_updated
        })

    def parse_current_outage(self, data: Optional[dict]) -> Optional[CurrentOutage]:
        if not data:
            return None
        return CurrentOutage(
            status=OutageStatus(data.get('status', OutageStatus.POWER_ON.value)),
            reason=data.get('reason', ''),
            start_time=data.get('start_time', ''),
            restoration_time=data.get('restoration_time', ''),
            last_updated=data.get('last_updated', '')
        )

    def parse_day_schedule(self, data: Optional[dict]) -> Optional[DaySchedule]:
        if not data or 'slots' not in data:
            return None
        return DaySchedule(
            date=data.get('date', ''),
            slots=[
                HourSlot(hour=s['hour'], status=CellStatus(s['status']))
                for s in data['slots']
            ],
            last_updated=data.get('last_updated', '')
        )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            logger.info("💾 Database closed")
            self.pool = None

    @property
    def is_connected(self) -> bool:
        return self.pool is not None
