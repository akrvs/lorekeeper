"""Raw source documents (provenance) + ingestion bookkeeping.

`raw_documents` is the immutable landing zone for everything we pull from
connectors (a GitHub PR payload, a Slack thread, a Notion page). Graph nodes
are *derived* from these and link back via `node_mentions`, so every assertion
in the graph is traceable to a real artifact with a URL — which is exactly what
the MCP layer returns when an agent asks "show me the thread".
"""

from datetime import datetime

from sqlalchemy import Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class RawDocument(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "raw_documents"

    source_system: Mapped[str] = mapped_column(Text, nullable=False)  # github | slack | notion
    source_type: Mapped[str] = mapped_column(Text, nullable=False)  # pull_request | message | ...
    external_id: Mapped[str] = mapped_column(Text, nullable=False)  # id in the source system

    # The RBAC anchor (Track 1): the access-controlled resource this doc belongs
    # to within its source — repo "owner/name", Slack/Teams channel id, vault, etc.
    resource_key: Mapped[str | None] = mapped_column(Text)

    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)  # extracted plain text
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)  # full original API object

    # sha256 of `content`; lets the pipeline skip re-extraction on unchanged docs.
    content_hash: Mapped[str | None] = mapped_column(Text)

    source_created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ingested_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        # One row per artifact per source — enables idempotent upserts.
        UniqueConstraint(
            "source_system",
            "source_type",
            "external_id",
            name="uq_raw_documents_identity",
        ),
        Index("ix_raw_documents_source", "source_system", "source_type"),
        Index("ix_raw_documents_resource", "source_system", "resource_key"),
    )


class IngestionRun(UUIDPKMixin, TimestampMixin, Base):
    """One execution of a connector. Stores the cursor for incremental sync."""

    __tablename__ = "ingestion_runs"

    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    connector: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="running"
    )  # running|completed|failed
    cursor: Mapped[str | None] = mapped_column(Text)  # pagination / since-token
    stats: Mapped[dict | None] = mapped_column(JSONB)  # {documents: n, nodes: n, edges: n}
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
