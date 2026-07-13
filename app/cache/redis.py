"""Redis-backed TTL cache (opt-in for production: CACHE_BACKEND=redis).

Uses the synchronous redis client (the MCP tools are sync). `redis` is an
optional dependency — imported lazily so the default in-memory path never needs it.
"""


class RedisCache:
    def __init__(self, url: str) -> None:
        try:
            import redis  # noqa: PLC0415 — optional dependency, imported lazily
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "CACHE_BACKEND=redis requires the 'redis' package (pip install redis)."
            ) from exc
        self._client = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> str | None:
        return self._client.get(key)

    def set(self, key: str, value: str, ttl: int) -> None:
        self._client.set(key, value, ex=ttl)

    def version(self, namespace: str) -> int:
        raw = self._client.get(f"cb:ver:{namespace}")
        return int(raw) if raw else 0

    def bump(self, namespace: str) -> None:
        self._client.incr(f"cb:ver:{namespace}")
