"""Compliance audit logging — who queried what.

Writes a durable `audit_log` row per principal-attributed tool call (and emits a
structured stderr line). Audit failures never break a query: a query that
succeeded must still return even if the audit insert fails.
"""

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models.security import AuditLog
from app.security.principal import Principal

logger = logging.getLogger("company_brain.security.audit")


class AuditLogger:
    def __init__(self, db: Session):
        self.db = db

    def record(
        self,
        principal: Principal,
        tool: str,
        params: dict | None,
        result_ids: Iterable[uuid.UUID] | None = None,
    ) -> None:
        ids = [str(i) for i in (result_ids or [])]
        logger.info(
            "AUDIT subject=%s tool=%s params=%s results=%d",
            principal.subject,
            tool,
            params,
            len(ids),
        )
        try:
            self.db.add(
                AuditLog(
                    subject=principal.subject,
                    groups=list(principal.groups),
                    tool=tool,
                    params=params,
                    result_node_ids=ids,
                    result_count=len(ids),
                    occurred_at=datetime.now(UTC),
                )
            )
            self.db.commit()
        except Exception:  # noqa: BLE001 — auditing must not break the query path
            self.db.rollback()
            logger.exception("Failed to persist audit log (query result still returned)")
