"""Edges — directed, typed relationships between nodes.

Every edge carries a `relationship` term (FK into the ontology registry), an
optional JSONB `properties` bag, and confidence/weight scores produced by the
extraction layer. `evidence_document_id` ties the edge back to the raw document
it was inferred from, so the graph can always answer "why do you believe this?".
"""

import uuid

from sqlalchemy import Float, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class Edge(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "edges"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )

    relationship_type: Mapped[str] = mapped_column(
        "relationship",  # column name in the DB (kept literal for readability)
        Text,
        ForeignKey("ontology_relationship_types.name", ondelete="RESTRICT"),
        nullable=False,
    )

    properties: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    weight: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))

    # The raw document this relationship was extracted from (audit trail).
    evidence_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_documents.id", ondelete="SET NULL")
    )

    source = relationship("Node", foreign_keys=[source_id])
    target = relationship("Node", foreign_keys=[target_id])

    __table_args__ = (
        # Collapse duplicate assertions of the same fact; re-ingestion upserts
        # (e.g. bumping confidence) instead of inserting a second edge.
        UniqueConstraint(
            "source_id",
            "target_id",
            "relationship",
            name="uq_edges_identity",
        ),
        Index("ix_edges_source_id", "source_id"),
        Index("ix_edges_target_id", "target_id"),
        Index("ix_edges_relationship", "relationship"),
        Index("ix_edges_properties", "properties", postgresql_using="gin"),
    )
