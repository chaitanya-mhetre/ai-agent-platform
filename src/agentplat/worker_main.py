"""Standalone worker process: `python -m agentplat.worker_main`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

from agentplat.config import Settings
from agentplat.container import build_container


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    c = build_container(settings, worker_id=f"{socket.gethostname()}-{os.getpid()}")
    if settings.auto_create_schema:
        await c.store.create_schema()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await c.worker().run_forever(stop)
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
