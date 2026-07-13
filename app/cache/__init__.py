"""Optional TTL cache for hot, idempotent reads (Track 3).

In-memory by default (zero-config local deploys); Redis opt-in for production
(`CACHE_BACKEND=redis`). The cache is namespaced+versioned so an ingestion run
invalidates stale reads via `bump()`, with TTL as the staleness backstop.

Security note: cache keys always include the caller's `Principal.scope_key()`, so
two principals with different visibility can never share a cached result.
"""

import hashlib
from functools import lru_cache
from typing import Protocol, runtime_checkable

from app.config import settings


@runtime_checkable
class Cache(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl: int) -> None: ...
    def version(self, namespace: str) -> int: ...
    def bump(self, namespace: str) -> None: ...


def make_key(namespace: str, version: int, *parts: str) -> str:
    raw = "|".join([namespace, str(version), *parts])
    return f"cb:{namespace}:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


@lru_cache
def get_cache() -> Cache:
    if settings.cache_backend.lower() == "redis":
        from app.cache.redis import RedisCache

        return RedisCache(settings.redis_url)
    from app.cache.memory import InMemoryTTLCache

    return InMemoryTTLCache()


__all__ = ["Cache", "make_key", "get_cache"]
