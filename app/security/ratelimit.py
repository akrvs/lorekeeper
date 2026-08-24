"""Dependency-free sliding-window rate limiter for the write-heavy routes.

One bucket per client host per protected prefix. In-memory on purpose: the
deployment is a single FastAPI process behind a reverse proxy, and a Redis
round-trip per request would defeat the point.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    WINDOW_SECONDS = 60.0

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, now: float | None = None) -> bool:
        if limit <= 0:
            return True
        moment = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and moment - bucket[0] > self.WINDOW_SECONDS:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(moment)
            return True

    def prune(self, now: float | None = None) -> None:
        """Drop buckets idle past the window (call occasionally to bound memory)."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            stale = [
                key
                for key, bucket in self._hits.items()
                if not bucket or moment - bucket[-1] > self.WINDOW_SECONDS * 2
            ]
            for key in stale:
                del self._hits[key]
