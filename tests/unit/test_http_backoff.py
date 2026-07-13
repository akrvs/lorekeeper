"""Connector resilience: continuous 429/503 must back off then give up."""

from unittest.mock import AsyncMock

import httpx
import pytest

import app.connectors._http as http_mod
from app.connectors._http import ConnectorHTTPError, request_with_retries


async def test_continuous_429_then_503_exhausts_and_raises(mocker):
    # Count backoff sleeps without actually waiting.
    sleep_mock = mocker.patch.object(http_mod.asyncio, "sleep", new=AsyncMock())
    statuses = [429, 429, 503, 503, 503, 503, 503]

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(statuses.pop(0), headers={"Retry-After": "0"}, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ConnectorHTTPError):
            await request_with_retries(client, "GET", "https://example.test/x")

    # http_max_retries defaults to 5 → 6 attempts, 5 backoff sleeps before giving up.
    assert sleep_mock.await_count == 5


async def test_recovers_after_transient_429(mocker):
    mocker.patch.object(http_mod.asyncio, "sleep", new=AsyncMock())
    statuses = [429, 200]

    def handler(_req: httpx.Request) -> httpx.Response:
        code = statuses.pop(0)
        return httpx.Response(code, headers={"Retry-After": "0"}, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await request_with_retries(client, "GET", "https://example.test/x")
    assert resp.status_code == 200
