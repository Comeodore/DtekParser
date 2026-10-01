"""Entry point: `python -m app`. One process, one socket per source port."""
import asyncio
import logging
import socket
import sys

import uvicorn

from app.api import create_app
from app.config import ConfigError, load_settings
from app.service import Service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpcore", "telegram", "uvicorn.access", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("app")


def _listen(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.set_inheritable(True)
    return sock


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as e:
        logger.error(f"Configuration error: {e}")
        return 2

    app = create_app(Service(settings))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=15))
    ports = sorted(s.port for s in settings.sources)
    logger.info(f"Listening on ports {ports}")
    asyncio.run(server.serve(sockets=[_listen(p) for p in ports]))
    # uvicorn returns normally when the lifespan startup failed.
    return 0 if server.started else 1


if __name__ == "__main__":
    sys.exit(main())
