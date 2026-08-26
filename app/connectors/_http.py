"""Async HTTP plumbing shared by the connectors.

`request_with_retries` wraps a single httpx request with exponential backoff +
jitter for the failure modes real APIs throw at you:
  * network drops / timeouts           -> retry
  * HTTP 429 (rate limited)            -> honor Retry-After, else backoff
  * HTTP 403 with rate-limit headers   -> GitHub's secondary rate limit
  * HTTP 5xx                           -> retry
  * HTTP 401 / 403 (real auth failure) -> fail fast (no point retrying)
  * other 4xx                          -> fail fast
"""

import asyncio
import logging
import random
import time

import httpx

from app.config import settings

logger = logging.getLogger("company_brain.connectors.http")


class ConnectorError(RuntimeError):
    """Generic, non-recoverable connector failure."""


class ConnectorAuthError(ConnectorError):
    """Bad/missing token or insufficient scopes."""


class ConnectorHTTPError(ConnectorError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def assert_upstream_url(url: str, base_url: str, *, allowed_suffixes: tuple[str, ...] = ()) -> str:
    """Validate an upstream-supplied URL (pagination links, download targets).

    A malicious or compromised API response could otherwise point the
    connector - and its Authorization header - at an internal address.
    Relative URLs are fine (the client resolves them against the pinned base);
    absolute URLs must land on the pinned host or an explicitly allowed
    suffix. Returns the url unchanged when acceptable.
    """
    candidate = httpx.URL(url)
    if candidate.host is None:
        return url
    expected = httpx.URL(base_url).host
    if candidate.host == expected:
        return url
    if any(candidate.host.endswith(suffix) for suffix in allowed_suffixes):
        return url
    raise ConnectorError(
        f"upstream returned a cross-host url ({candidate.scheme}://{candidate.host}); "
        "refusing to follow it"
    )


def build_async_client(
    base_url: str,
    headers: dict[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float | None = None,
) -> httpx.AsyncClient:
    """Construct an AsyncClient. `transport` is injectable for tests (MockTransport)."""
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout or settings.http_timeout_seconds,
        transport=transport,
        follow_redirects=True,
    )


def _backoff(attempt: int) -> float:
    base = min(settings.http_backoff_max_seconds, settings.http_backoff_base_seconds * (2**attempt))
    return base + random.uniform(0.0, base * 0.25)  # full-ish jitter


def _retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    """Prefer Retry-After, then X-RateLimit-Reset; cap so a demo never hangs."""
    cap = settings.http_backoff_max_seconds
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            return min(float(ra), cap)
        except ValueError:
            pass
    reset = resp.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return max(0.0, min(float(reset) - time.time(), cap))
        except ValueError:
            pass
    return _backoff(attempt)


def _is_secondary_rate_limit(resp: httpx.Response) -> bool:
    # GitHub returns 403 (not 429) for secondary/abuse rate limits.
    return resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    data: dict | None = None,
) -> httpx.Response:
    max_retries = settings.http_max_retries
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = await client.request(method, url, params=params, json=json, data=data)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt >= max_retries:
                raise ConnectorError(f"network error after {attempt + 1} attempts: {exc}") from exc
            wait = _backoff(attempt)
            logger.warning("network error on %s (%s); retrying in %.1fs", url, exc, wait)
            await asyncio.sleep(wait)
            continue

        status = resp.status_code

        if status < 300:
            return resp

        if status == 429 or (status == 403 and _is_secondary_rate_limit(resp)):
            if attempt >= max_retries:
                raise ConnectorHTTPError(status, "rate limited; retries exhausted")
            wait = _retry_after_seconds(resp, attempt)
            logger.warning("rate limited (%s) on %s; retrying in %.1fs", status, url, wait)
            await asyncio.sleep(wait)
            continue

        if status in (401, 403):
            raise ConnectorAuthError(
                f"authentication/authorization failed ({status}) on {url}. "
                "Check the token and its scopes."
            )

        if status >= 500:
            if attempt >= max_retries:
                raise ConnectorHTTPError(status, "server error; retries exhausted")
            wait = _backoff(attempt)
            logger.warning("server error %s on %s; retrying in %.1fs", status, url, wait)
            await asyncio.sleep(wait)
            continue

        # Other 4xx (404, 422, ...) — not recoverable.
        raise ConnectorHTTPError(status, resp.text[:300])

    raise ConnectorError(f"request to {url} failed: {last_exc}")
