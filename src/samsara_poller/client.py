from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import Settings
from .errors import PermanentSamsaraError, ResponseSchemaError, RetryableSamsaraError
from .reliability import AsyncRateLimiter, parse_retry_after, retry_delay

logger = logging.getLogger(__name__)


class SamsaraClient:
    RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._limiter = AsyncRateLimiter(settings.requests_per_second)
        self._http = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/json",
                "User-Agent": "samsara-poller/0.1.0",
            },
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            limits=httpx.Limits(
                max_connections=settings.concurrency,
                max_keepalive_connections=settings.concurrency,
            ),
        )
        self._closed = False

    async def __aenter__(self) -> "SamsaraClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._http.aclose()

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("SamsaraClient is closed")
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("API path must be an absolute path on the configured host")

        for attempt in range(self._settings.max_attempts):
            retry_after: float | None = None
            try:
                await self._limiter.acquire()
                response = await self._http.get(path, params=params)

                if response.status_code in self.RETRYABLE_STATUS:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    error = RetryableSamsaraError(
                        f"Retryable HTTP status {response.status_code}",
                        retry_after,
                    )
                    raise error

                if response.is_error:
                    # Do not include response bodies; they may contain sensitive data.
                    raise PermanentSamsaraError(
                        f"Non-retryable HTTP status {response.status_code} for {path}"
                    )

                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ResponseSchemaError("Response was not valid JSON") from exc
                if not isinstance(payload, dict):
                    raise ResponseSchemaError("Expected a JSON object response")
                return payload

            except asyncio.CancelledError:
                raise
            except RetryableSamsaraError as exc:
                error: Exception = exc
                retry_after = exc.retry_after
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                error = exc
            except (PermanentSamsaraError, ResponseSchemaError):
                raise
            except httpx.HTTPError as exc:
                # Unknown HTTP client failures are not blindly retried.
                raise PermanentSamsaraError(f"HTTP client error for {path}: {type(exc).__name__}") from exc

            if attempt + 1 >= self._settings.max_attempts:
                raise RetryableSamsaraError(
                    f"Request failed after {self._settings.max_attempts} attempts: {type(error).__name__}"
                ) from error

            delay = await retry_delay(attempt, retry_after)
            logger.warning(
                "request_retry path=%s attempt=%d delay_seconds=%.2f error_type=%s",
                path, attempt + 1, delay, type(error).__name__,
            )
            await asyncio.sleep(delay)

        raise AssertionError("Retry loop exhausted unexpectedly")
