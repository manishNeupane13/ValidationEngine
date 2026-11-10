from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    base_url: str = "https://api.samsara.com"
    poll_interval_seconds: float = 60.0
    request_timeout_seconds: float = 20.0
    max_attempts: int = 4
    concurrency: int = 5
    requests_per_second: float = 5.0
    vehicle_page_size: int = 512
    # Conservative default until the endpoint contract is verified.
    vehicle_batch_size: int = 1
    output_dir: Path = Path("./data")
    stats_types: tuple[str, ...] = ("engineStates",)

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("SAMSARA_API_TOKEN", "").strip()
        if not token:
            raise ValueError("SAMSARA_API_TOKEN must be set")
        settings = cls(
            token=token,
            base_url=os.getenv("SAMSARA_BASE_URL", "https://api.samsara.com"),
            poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
            max_attempts=int(os.getenv("MAX_ATTEMPTS", "4")),
            concurrency=int(os.getenv("CONCURRENCY", "5")),
            requests_per_second=float(os.getenv("REQUESTS_PER_SECOND", "5")),
            vehicle_page_size=int(os.getenv("VEHICLE_PAGE_SIZE", "512")),
            vehicle_batch_size=int(os.getenv("VEHICLE_BATCH_SIZE", "1")),
            output_dir=Path(os.getenv("OUTPUT_DIR", "./data")),
            stats_types=tuple(
                item.strip()
                for item in os.getenv("SAMSARA_STATS_TYPES", "engineStates").split(",")
                if item.strip()
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.base_url.startswith("https://"):
            raise ValueError("SAMSARA_BASE_URL must use HTTPS")
        if self.poll_interval_seconds <= 0:
            raise ValueError("POLL_INTERVAL_SECONDS must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("REQUEST_TIMEOUT_SECONDS must be positive")
        if self.max_attempts < 1:
            raise ValueError("MAX_ATTEMPTS must be at least 1")
        if self.concurrency < 1 or self.vehicle_page_size < 1:
            raise ValueError("CONCURRENCY and VEHICLE_PAGE_SIZE must be positive")
        if self.requests_per_second <= 0:
            raise ValueError("REQUESTS_PER_SECOND must be positive")
        if self.vehicle_batch_size < 1:
            raise ValueError("VEHICLE_BATCH_SIZE must be at least 1")
