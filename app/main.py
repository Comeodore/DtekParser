import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.routes.schedule import router as schedule_router
from app.scheduler import schedule_service

APP_VERSION = "2.0.0"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 DTEK Schedule API starting...")
    logger.info(f"📍 Parser: {schedule_service.parser_type.upper()}, Address: {schedule_service.address}")

    try:
        await schedule_service.start()
    except Exception as e:
        logger.error(f"❌ Failed to start schedule service: {e}")
        raise

    yield

    try:
        await schedule_service.stop()
    except Exception as e:
        logger.error(f"❌ Error stopping schedule service: {e}")

    logger.info("👋 DTEK Schedule API stopped")


app = FastAPI(
    title="DTEK Schedule API",
    description="API for DTEK power outage schedules",
    version=APP_VERSION,
    lifespan=lifespan
)

class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        
        response.headers["X-App-Version"] = APP_VERSION
        return response


app.add_middleware(CacheControlMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(schedule_router)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
async def webapp():
    html_path = STATIC_DIR / "index.html"
    content = html_path.read_text(encoding="utf-8")
    
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-App-Version": APP_VERSION,
            "Cache-Tag": f"webapp,v{APP_VERSION}"
        }
    )


@app.get("/api")
async def api_info():
    return {"service": "DTEK Schedule API", "version": APP_VERSION, "docs": "/docs"}
