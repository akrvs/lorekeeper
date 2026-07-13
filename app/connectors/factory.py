"""ConnectorFactory — the rigid registration point for ingestion drivers (Track 4).

Connectors self-register with `@ConnectorFactory.register("name")`. Adding a new
source requires touching zero existing files: the factory, the pipeline, and the
/ingest API all discover it through the registry.
"""

from sqlalchemy.orm import Session

from app.connectors.base import BaseConnector


class ConnectorFactory:
    _registry: dict[str, type[BaseConnector]] = {}

    @classmethod
    def register(cls, name: str):
        def decorator(klass: type[BaseConnector]) -> type[BaseConnector]:
            klass.source_system = name
            cls._registry[name] = klass
            return klass

        return decorator

    @classmethod
    def create(cls, db: Session, source: str, **overrides) -> BaseConnector:
        if source not in cls._registry:
            raise ValueError(f"Unknown source {source!r}. Available: {cls.available()}")
        # Connectors ignore overrides they don't use (their __init__ accepts **_),
        # and fall back to settings for anything not supplied.
        clean = {k: v for k, v in overrides.items() if v is not None}
        return cls._registry[source](db, **clean)

    @classmethod
    def available(cls) -> list[str]:
        return sorted(cls._registry)
