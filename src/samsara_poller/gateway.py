from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .client import SamsaraClient
from .config import Settings
from .errors import ResponseSchemaError
from .models import Vehicle


class SamsaraVehicleGateway:
    """Maps the external Samsara contract to application-level operations."""

    def __init__(self, client: SamsaraClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def list_vehicles(self) -> list[Vehicle]:
        # Assumption: GET /fleet/vehicles supports cursor pagination and returns
        # {"data": [{"id": "..."}], "pagination": {"endCursor": "...", "hasNextPage": true}.
        # Verify these fields against the API version enabled for your account.
        vehicles: dict[str, Vehicle] = {}
        after: str | None = None
        seen_cursors: set[str] = set()

        while True:
            params: dict[str, Any] = {"limit": self._settings.vehicle_page_size}
            if after:
                params["after"] = after
            payload = await self._client.get_json("/fleet/vehicles", params=params)
            records = payload.get("data")
            if not isinstance(records, list):
                raise ResponseSchemaError("/fleet/vehicles response must include a data list")
            try:
                for item in records:
                    vehicle = Vehicle.model_validate(item)
                    vehicles[vehicle.id] = vehicle
            except (ValidationError, TypeError) as exc:
                raise ResponseSchemaError("Invalid vehicle record in /fleet/vehicles") from exc

            pagination = payload.get("pagination") or {}
            if not isinstance(pagination, dict):
                raise ResponseSchemaError("pagination must be an object")
            has_next = pagination.get("hasNextPage", False)
            if not isinstance(has_next, bool):
                raise ResponseSchemaError("pagination.hasNextPage must be a boolean")
            if not has_next:
                break
            next_cursor = pagination.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ResponseSchemaError("Missing endCursor while hasNextPage is true")
            if next_cursor in seen_cursors:
                raise ResponseSchemaError("Pagination cursor repeated; refusing infinite loop")
            seen_cursors.add(next_cursor)
            after = next_cursor

        return list(vehicles.values())

    async def get_vehicle_stats(self, vehicle_ids: list[str]) -> dict[str, Any]:
        # Assumption: stats endpoint accepts comma-separated vehicleIds and types.
        # Default batch size remains 1 until this is verified for your endpoint/version.
        return await self._client.get_json(
            "/fleet/vehicles/stats",
            params={
                "vehicleIds": ",".join(vehicle_ids),
                "types": ",".join(self._settings.stats_types),
            },
        )
