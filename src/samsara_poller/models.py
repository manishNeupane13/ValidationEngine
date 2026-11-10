from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Vehicle(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str = Field(min_length=1)


class PollRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    record_type: str = "vehicle_stats"
    observed_at: datetime
    vehicle_id: str
    data: dict[str, Any]


class FailureRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    failed_at: datetime
    operation: str
    vehicle_ids: list[str] = Field(default_factory=list)
    error_type: str
    message: str
    attempts: int = 1
