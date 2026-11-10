from __future__ import annotations

import asyncio
import random
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class AsyncRateLimiter:
    """Simple process-local minimum-spacing limiter shared by requests."""

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._spacing = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._spacing
        if delay:
            await asyncio.sleep(delay)


async def retry_delay(
    attempt_index: int,
    retry_after: float | None,
    max_delay: float = 30.0,
) -> float:
    # Retry-After is a server instruction: do not cap it and retry earlier.
    if retry_after is not None:
        return retry_after
    exponential = min(max_delay, 0.5 * (2**attempt_index))
    return min(max_delay, exponential + random.uniform(0.0, min(1.0, exponential * 0.2)))
