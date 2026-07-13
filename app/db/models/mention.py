"""node_mentions — provenance bridge between graph nodes and raw documents.

Many-to-many: a node (e.g. the "checkout-v2" feature) can be mentioned across
many documents, and a document mentions many nodes. This is the index the MCP
layer walks to return the *actual source artifacts* (Slack threads, PRs) behind
any entity the agent asks about.
"""

import uuid

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class NodeMention(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "node_mentions"

    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_documents.id", ondelete="CASCADE"), nullable=False
    )
    # The snippet of text in the document that produced this mention.
    context: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("node_id", "document_id", name="uq_node_mentions_identity"),
        Index("ix_node_mentions_node_id", "node_id"),
        Index("ix_node_mentions_document_id", "document_id"),
    )
