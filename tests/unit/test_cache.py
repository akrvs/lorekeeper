"""In-memory TTL cache: expiry + namespace versioning."""

import time

from app.cache import make_key
from app.cache.memory import InMemoryTTLCache


def test_ttl_expiry(monkeypatch):
    cache = InMemoryTTLCache()
    cache.set("k", "v", ttl=10)
    assert cache.get("k") == "v"
    # Jump past the TTL. Capture the real clock first so the patched lambda
    # doesn't recurse into itself (app.cache.memory.time IS this `time` module).
    real_monotonic = time.monotonic
    monkeypatch.setattr("app.cache.memory.time.monotonic", lambda: real_monotonic() + 100)
    assert cache.get("k") is None


def test_namespace_versioning():
    cache = InMemoryTTLCache()
    assert cache.version("graph") == 0
    cache.bump("graph")
    assert cache.version("graph") == 1


def test_make_key_includes_scope_and_version():
    a = make_key("graph", 1, "search", "scopeA", "q")
    b = make_key("graph", 1, "search", "scopeB", "q")  # different principal scope
    c = make_key("graph", 2, "search", "scopeA", "q")  # different version
    assert a != b and a != c
