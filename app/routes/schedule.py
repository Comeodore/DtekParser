import asyncio
import logging

from fastapi import APIRouter
from typing import Optional

from app.models import ScheduleResponse, DaySchedule, CurrentOutage, HealthResponse
from app.scheduler import schedule_service

logger = logging.getLogger(__name__)

PC_MAC_ADDRESS = "BC:FC:E7:1A:15:90"

router = APIRouter(prefix="/api/v1", tags=["schedule"])


@router.get("/schedule", response_model=ScheduleResponse)
async def get_schedule() -> ScheduleResponse:
    data = await schedule_service.get_current_schedule()

    if not data:
        return ScheduleResponse(address=schedule_service.address, parser_type=schedule_service.parser_type)

    return ScheduleResponse(
        current_outage=schedule_service.db.parse_current_outage(data.get('current_outage')),
        today=schedule_service.db.parse_day_schedule(data.get('today')),
        tomorrow=schedule_service.db.parse_day_schedule(data.get('tomorrow')),
        updated_at=data.get('updated_at'),
        address=schedule_service.address,
        parser_type=schedule_service.parser_type
    )


@router.get("/schedule/today", response_model=Optional[DaySchedule])
async def get_today_schedule() -> Optional[DaySchedule]:
    data = await schedule_service.get_current_schedule()
    if not data:
        return None
    return schedule_service.db.parse_day_schedule(data.get('today'))


@router.get("/schedule/tomorrow", response_model=Optional[DaySchedule])
async def get_tomorrow_schedule() -> Optional[DaySchedule]:
    data = await schedule_service.get_current_schedule()
    if not data:
        return None
    return schedule_service.db.parse_day_schedule(data.get('tomorrow'))


@router.get("/status", response_model=Optional[CurrentOutage])
async def get_current_status() -> Optional[CurrentOutage]:
    data = await schedule_service.get_current_schedule()
    if not data:
        return None
    return schedule_service.db.parse_current_outage(data.get('current_outage'))


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(
        status="ok" if schedule_service.is_running else "degraded",
        database=schedule_service.db.is_connected,
        parser=schedule_service.parser.is_running,
        last_parse=schedule_service.last_parse
    )


@router.post("/wol")
async def wake_on_lan():
    try:
        process = await asyncio.create_subprocess_exec(
            "wakeonlan", PC_MAC_ADDRESS,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()

        if process.returncode == 0:
            logger.info(f"WoL packet sent to {PC_MAC_ADDRESS}")
            return {"status": "ok"}

        error = stderr.decode().strip() if stderr else "Unknown error"
        logger.error(f"WoL failed: {error}")
        return {"status": "error", "detail": error}
    except FileNotFoundError:
        logger.error("wakeonlan not found")
        return {"status": "error", "detail": "wakeonlan not installed"}
    except Exception as e:
        logger.error(f"WoL error: {e}")
        return {"status": "error", "detail": str(e)}
