"""Entry point: `python -m deye_reserve`."""
import asyncio
import logging
import signal
import sys

import aiohttp
from aiohttp import web

from deye_reserve.config import ConfigError, load_config
from deye_reserve.deye import DeyeClient
from deye_reserve.net import HttpSources, Solcast, Telegram
from deye_reserve.service import ReserveService
from deye_reserve.web import make_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logger = logging.getLogger("deye_reserve")


async def serve() -> None:
    config = load_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        deye = DeyeClient(config.base_url, config.app_id, config.app_secret, config.email, config.password_sha256,
                          session, config.data_dir / "token.json")
        solcast = (Solcast(config.solcast_key, config.solcast_site, session)
                   if config.solcast_key and config.solcast_site else None)
        service = ReserveService(config, HttpSources(config, session), deye,
                                 Telegram(config.bot_token, config.chat_id, session, config.dry_run),
                                 solcast=solcast)
        runner = web.AppRunner(make_app(service), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, config.host, config.port).start()
        logger.info(f"deye-reserve on {config.host}:{config.port}: group {config.group}, "
                    f"write {'ON' if config.write else 'off'}, solcast {'on' if solcast else 'off'}, "
                    f"dry_run={config.dry_run}")
        main = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, main.cancel)
        try:
            await service.run()
        except asyncio.CancelledError:
            logger.info("stopping")
        finally:
            await runner.cleanup()


def main() -> int:
    try:
        asyncio.run(serve())
    except ConfigError as e:
        logger.error(f"Configuration error: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
