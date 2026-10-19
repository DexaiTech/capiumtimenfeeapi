"""Worker entry point: ``python -m app.workers.main``."""

import asyncio
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine, get_sessionmaker
from app.workers.runner import Worker


async def main() -> None:
    settings = get_settings()
    configure_logging(debug=settings.app_debug, service="capium-time-fees-worker")
    worker = Worker(get_sessionmaker(), settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # Windows event loops: Ctrl+C cancels asyncio.run instead
            break
    try:
        await worker.run_forever()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
