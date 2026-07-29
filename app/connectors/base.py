"""Base connector: fetch source artifacts -> raw_documents + ingestion_runs.

A connector only has to implement `fetch()`, yielding `RawDoc` objects. The base
class handles the boring-but-important parts: opening an `IngestionRun`,
idempotently upserting each document (on the natural key, with a content hash so
unchanged docs are cheap), recording stats, and closing the run.
"""

import asyncio
import concurrent.futures
import hashlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Coroutine, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.source import IngestionRun, RawDocument

logger = logging.getLogger("company_brain.connectors")


def run_blocking[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine to completion from a sync caller.

    Connectors do async network I/O but the orchestration/DB layer is sync. This
    bridges the two: normally `asyncio.run`, but if a loop is already running
    (the connector was invoked from async code) it offloads to a worker thread
    so we never hit 'asyncio.run() cannot be called from a running event loop'.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class RawDoc:
    """A normalized artifact a connector emits, before DB insertion."""

    source_type: str
    external_id: str
    title: str | None = None
    url: str | None = None
    author: str | None = None
    content: str | None = None
    raw_payload: dict | None = field(default=None)
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None
    # RBAC anchor (Track 1): the access-controlled resource within the source
    # (repo "owner/name", channel id, vault, …). Connectors set this.
    resource_key: str | None = None


class BaseConnector(ABC):
    #: e.g. "github" | "slack" | "notion" — must match values in raw_documents.
    source_system: str
    #: The synced resource (repo, channel id, ...) — scopes the sync cursor.
    resource_key: str | None = None
    #: Incremental-sync token computed by fetch(); persisted on the run record.
    new_cursor: str | None = None

    def __init__(self, db: Session):
        self.db = db

    @abstractmethod
    def fetch(self) -> Iterable[RawDoc]:
        """Yield artifacts from the source system."""
        raise NotImplementedError

    def last_cursor(self) -> str | None:
        """The cursor persisted by the newest completed run for this resource."""
        return self.db.scalar(
            select(IngestionRun.cursor)
            .where(
                IngestionRun.source_system == self.source_system,
                IngestionRun.resource_key == self.resource_key,
                IngestionRun.status == "completed",
                IngestionRun.cursor.is_not(None),
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )

    def run(self) -> tuple[IngestionRun, list[RawDocument]]:
        """Execute one ingestion pass. Returns the run record and upserted docs."""
        run = IngestionRun(
            source_system=self.source_system,
            connector=type(self).__name__,
            resource_key=self.resource_key,
            status="running",
            started_at=_utcnow(),
        )
        self.db.add(run)
        self.db.flush()

        documents: list[RawDocument] = []
        try:
            for raw in self.fetch():
                documents.append(self._upsert_document(raw))
            run.status = "completed"
            run.stats = {"documents": len(documents)}
            run.cursor = self.new_cursor
            run.finished_at = _utcnow()
            self.db.commit()
        except Exception as exc:  # noqa: BLE001 — record failure, then re-raise
            self.db.rollback()
            run.status = "failed"
            run.error = str(exc)
            run.finished_at = _utcnow()
            self.db.add(run)
            self.db.commit()
            logger.exception("Ingestion run failed for %s", self.source_system)
            raise

        logger.info("%s ingested %d documents", self.source_system, len(documents))
        return run, documents

    def _upsert_document(self, raw: RawDoc) -> RawDocument:
        content_hash = (
            hashlib.sha256(raw.content.encode("utf-8")).hexdigest() if raw.content else None
        )
        stmt = pg_insert(RawDocument).values(
            source_system=self.source_system,
            source_type=raw.source_type,
            external_id=raw.external_id,
            resource_key=raw.resource_key,
            url=raw.url,
            title=raw.title,
            author=raw.author,
            content=raw.content,
            raw_payload=raw.raw_payload,
            content_hash=content_hash,
            source_created_at=raw.source_created_at,
            source_updated_at=raw.source_updated_at,
            ingested_at=_utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_raw_documents_identity",
            set_={
                "resource_key": stmt.excluded.resource_key,
                "url": stmt.excluded.url,
                "title": stmt.excluded.title,
                "author": stmt.excluded.author,
                "content": stmt.excluded.content,
                "raw_payload": stmt.excluded.raw_payload,
                "content_hash": stmt.excluded.content_hash,
                "source_updated_at": stmt.excluded.source_updated_at,
                "ingested_at": stmt.excluded.ingested_at,
            },
        ).returning(RawDocument)
        # populate_existing: the upsert bypasses the ORM, so an instance already
        # in the identity map (same-session re-sync) must be refreshed from the
        # returned row rather than served with stale attributes.
        return self.db.execute(stmt, execution_options={"populate_existing": True}).scalar_one()
