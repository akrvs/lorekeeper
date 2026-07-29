"""Process-local TTL cache (default backend). Thread-safe, no dependencies."""

import threading
import time


class InMemoryTTLCache:
    _PURGE_AT = 1024

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, str]] = {}
        self._versions: dict[str, int] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at < time.monotonic():
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: str, ttl: int) -> None:
        with self._lock:
            now = time.monotonic()
            if len(self._store) >= self._PURGE_AT:
                self._store = {k: v for k, v in self._store.items() if v[0] >= now}
            self._store[key] = (now + ttl, value)

    def version(self, namespace: str) -> int:
        with self._lock:
            return self._versions.get(namespace, 0)

    def bump(self, namespace: str) -> None:
        with self._lock:
            self._versions[namespace] = self._versions.get(namespace, 0) + 1
