"""Nodes — the entities of the knowledge graph (property-graph in relational form).

A single `nodes` table holds every entity type (user, repository, pull_request,
slack_thread, feature, deployment, incident, ...). Type-specific attributes live
in the schemaless `properties` JSONB column; the canonical text representation
is embedded into `embedding` for semantic search.

Why one table instead of a table per type:
- Graph traversal joins against `edges` stay uniform — no per-type UNIONs.
- A single HNSW index serves semantic search across all entity kinds.
- New entity types are an ontology INSERT, not a schema migration.
"""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Float, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class Node(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "nodes"

    # FK into the ontology registry → only known entity types are allowed.
    node_type: Mapped[str] = mapped_column(
        Text,
        ForeignKey("ontology_node_types.name", ondelete="RESTRICT"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)  # human label / canonical name
    summary: Mapped[str | None] = mapped_column(Text)  # LLM summary; source of the embedding
    properties: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # Semantic search vector. Nullable: a node may exist before it is embedded.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))

    # Provenance / natural key. external_id may be NULL for *derived* entities
    # (e.g. a "feature" the LLM inferred), which have no id in any source system.
    source_system: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)

    # Entity resolution: when dedup merges two nodes, the loser points at the
    # surviving canonical node. NULL == this node is itself canonical.
    canonical_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="SET NULL")
    )

    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))

    canonical = relationship("Node", remote_side="Node.id", uselist=False)

    __table_args__ = (
        # Idempotent upsert key for sourced entities. NULLs are distinct in
        # Postgres, so multiple derived nodes (external_id IS NULL) coexist and
        # are deduplicated by the ontology engine via name/embedding instead.
        UniqueConstraint(
            "node_type",
            "source_system",
            "external_id",
            name="uq_nodes_identity",
        ),
        Index("ix_nodes_node_type", "node_type"),
        Index("ix_nodes_source", "source_system", "external_id"),
        # GIN on JSONB for property filters (e.g. properties @> '{"status":"failed"}').
        Index("ix_nodes_properties", "properties", postgresql_using="gin"),
        # Trigram index for fuzzy name matching during entity dedup.
        Index(
            "ix_nodes_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # HNSW ANN index for cosine semantic search over embeddings.
        Index(
            "ix_nodes_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
