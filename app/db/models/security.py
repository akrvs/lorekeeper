"""Security models (Track 1): audit trail + group→resource grant policy.

`access_grants` is the data-driven RBAC policy: an IdP group maps to a
(source_system, resource_key) it may read (NULL resource_key == all resources in
that source). `audit_log` records every principal-attributed graph query for
compliance.
"""

from datetime import datetime

from sqlalchemy import Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin, UUIDPKMixin


class AccessGrant(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "access_grants"

    group_name: Mapped[str] = mapped_column(Text, nullable=False)  # IdP group / role
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL == every resource in this source_system.
    resource_key: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "group_name", "source_system", "resource_key", name="uq_access_grants_identity"
        ),
        Index("ix_access_grants_group", "group_name"),
    )


class AuditLog(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "audit_log"

    subject: Mapped[str] = mapped_column(Text, nullable=False)  # principal id
    groups: Mapped[list | None] = mapped_column(JSONB)
    tool: Mapped[str] = mapped_column(Text, nullable=False)  # mcp tool name
    params: Mapped[dict | None] = mapped_column(JSONB)
    result_node_ids: Mapped[list | None] = mapped_column(JSONB)
    result_count: Mapped[int] = mapped_column(default=0)
    occurred_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        Index("ix_audit_log_subject", "subject"),
        Index("ix_audit_log_tool", "tool"),
    )
