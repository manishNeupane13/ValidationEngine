from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .errors import SamsaraError
from .gateway import SamsaraVehicleGateway
from .models import FailureRecord, PollRecord, Vehicle
from .storage import JsonlFileStore

logger = logging.getLogger(__name__)


class VehiclePollingWorkflow:
    def __init__(
        self,
        gateway: SamsaraVehicleGateway,
        store: JsonlFileStore,
        settings: Settings,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.concurrency)

    @staticmethod
    def _chunks(items: list[Vehicle], size: int) -> list[list[Vehicle]]:
        return [items[index:index + size] for index in range(0, len(items), size)]

    async def _fetch_batch(self, batch: list[Vehicle]) -> bool:
        ids = [vehicle.id for vehicle in batch]
        async with self._semaphore:
            try:
                payload: dict[str, Any] = await self._gateway.get_vehicle_stats(ids)
                # Save the raw batch response once to avoid assuming the exact
                # per-vehicle response schema. One record per batch, with IDs.
                record = PollRecord(
                    observed_at=datetime.now(timezone.utc),
                    vehicle_id=",".join(ids),
                    data=payload,
                )
                await self._store.append_result(record)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("stats_batch_failed vehicle_count=%d", len(ids))
                failure = FailureRecord(
                    failed_at=datetime.now(timezone.utc),
                    operation="get_vehicle_stats",
                    vehicle_ids=ids,
                    error_type=type(exc).__name__,
                    message=str(exc)[:1000],
                    attempts=self._settings.max_attempts,
                )
                # If writing the failure journal fails, let the task fail visibly.
                await self._store.append_failure(failure)
                return False

    async def run_once(self) -> dict[str, int]:
        vehicles = await self._gateway.list_vehicles()
        batches = self._chunks(vehicles, self._settings.vehicle_batch_size)
        logger.info("poll_started vehicle_count=%d batch_count=%d", len(vehicles), len(batches))

        outcomes = await asyncio.gather(
            *(self._fetch_batch(batch) for batch in batches),
            return_exceptions=True,
        )
        fatal = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        if fatal:
            # Includes durable-write failures; don't silently report a healthy cycle.
            raise RuntimeError(f"{len(fatal)} batch task(s) failed unexpectedly") from fatal[0]

        failures = sum(outcome is False for outcome in outcomes)
        successes = len(outcomes) - failures
        logger.info("poll_complete vehicles=%d batches=%d succeeded=%d failed=%d",
                    len(vehicles), len(batches), successes, failures)
        return {"vehicles": len(vehicles), "batches": len(batches),
                "batch_successes": successes, "batch_failures": failures}
