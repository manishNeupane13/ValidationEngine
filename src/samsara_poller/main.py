from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from .client import SamsaraClient
from .config import Settings
from .gateway import SamsaraVehicleGateway
from .storage import JsonlFileStore
from .workflow import VehiclePollingWorkflow

logger = logging.getLogger(__name__)


async def serve(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    store = JsonlFileStore(settings.output_dir)
    await store.initialize()

    async with SamsaraClient(settings) as client:
        gateway = SamsaraVehicleGateway(client, settings)
        workflow = VehiclePollingWorkflow(gateway, store, settings)
        while not stop.is_set():
            try:
                await workflow.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One failed cycle must not terminate the long-running service.
                logger.exception("poll_cycle_failed")

            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.poll_interval_seconds
                )
            except TimeoutError:
                pass


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
