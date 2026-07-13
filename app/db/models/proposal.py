"""Proposals — the write choke point of the living ontology.

Every change the system wants to make to *itself* — merging duplicate entities,
evolving the schema, flagging stale facts — is recorded as a proposal row before
it happens. Maintenance agents produce proposals; the proposal engine applies
them once a human approves (or immediately, above the auto-apply confidence
threshold). Applied proposals carry a `rollback_data` snapshot, so any change
can be undone with one command.

This mirrors how `GraphRepository` is the single read choke point: proposals
are the single *mutation* choke point for self-maintenance.
"""

from datetime import datetime

from sqlalchemy import Float, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin

# Lifecycle: pending -> applied | rejected  (human review)
#            -> auto_applied                (confidence >= threshold at submit)
#            applied/auto_applied -> rolled_back
#            any apply attempt that raises -> failed (error retained for triage)
STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_AUTO_APPLIED = "auto_applied"
STATUS_REJECTED = "rejected"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_FAILED = "failed"

APPLIED_STATUSES = {STATUS_APPLIED, STATUS_AUTO_APPLIED}


class Proposal(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "proposals"

    kind: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. entity_merge
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=STATUS_PENDING)

    # Kind-specific change description; the handler for `kind` interprets it.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    # Which agent produced it ("dedup", "drift", ...; "human" for manual submits).
    agent: Mapped[str] = mapped_column(Text, nullable=False)
    # Supporting facts for the reviewer: document ids, similarity scores, etc.
    evidence: Mapped[dict | None] = mapped_column(JSONB)

    # Agent-computed identity of the proposed change (e.g. sorted node-id pair).
    # Stops rescans from re-filing the same proposal — including ones a human
    # already rejected, which is what makes rejection sticky.
    dedup_key: Mapped[str | None] = mapped_column(Text)

    reviewed_by: Mapped[str | None] = mapped_column(Text)  # principal subject
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Inverse-operation snapshot captured by the handler at apply time.
    rollback_data: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("kind", "dedup_key", name="uq_proposals_dedup"),
        Index("ix_proposals_status", "status"),
        Index("ix_proposals_kind", "kind"),
    )
