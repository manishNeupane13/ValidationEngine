from datetime import datetime, timezone

import httpx
import pytest

from samsara_poller.config import Settings
from samsara_poller.client import SamsaraClient
from samsara_poller.errors import PermanentSamsaraError, RetryableSamsaraError, ResponseSchemaError
from samsara_poller.reliability import AsyncRateLimiter, parse_retry_after


def settings(**overrides):
    values = dict(
        token="test-token",
        max_attempts=2,
        requests_per_second=10000,
        request_timeout_seconds=1,
    )
    values.update(overrides)
    return Settings(**values)


def test_parse_retry_after_seconds():
    assert parse_retry_after("3") == 3.0


def test_parse_invalid_retry_after():
    assert parse_retry_after("not-a-date") is None


@pytest.mark.asyncio
async def test_client_returns_json_and_closes():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
    client = SamsaraClient(settings())
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="https://api.samsara.com",
        transport=transport,
        headers={"Authorization": "Bearer test-token"},
    )
    assert await client.get_json("/fleet/vehicles") == {"data": []}
    await client.aclose()


@pytest.mark.asyncio
async def test_non_retryable_status_is_not_retried():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"message": "unauthorized"})

    client = SamsaraClient(settings())
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="https://api.samsara.com", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(PermanentSamsaraError):
        await client.get_json("/fleet/vehicles")
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_json_raises_schema_error():
    client = SamsaraClient(settings())
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="https://api.samsara.com",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"no-json")),
    )
    with pytest.raises(ResponseSchemaError):
        await client.get_json("/fleet/vehicles")
    await client.aclose()


@pytest.mark.asyncio
async def test_limiter_accepts_positive_rate():
    limiter = AsyncRateLimiter(1000)
    await limiter.acquire()
