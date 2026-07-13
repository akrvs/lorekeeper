"""Proposal engine — submit, review, apply, and roll back self-maintenance changes.

Handlers implement the three verbs for one proposal `kind` and self-register via
`@register_handler`, mirroring `ConnectorFactory`. The engine owns the lifecycle
and transactions; handlers own the graph mutations:

    submit()   -> pending row (or immediate auto-apply above the confidence
                  threshold; disabled by default — settings.proposal_auto_apply_threshold)
    approve()  -> handler.validate + handler.apply, snapshot stored in rollback_data
    reject()   -> sticky no (the dedup_key keeps agents from re-filing it)
    rollback() -> handler.rollback restores the snapshot

Each public method commits on success; a failed apply rolls the transaction back
and records the error on the proposal so agent misbehavior is visible, not silent.
"""

import logging
import uuid
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cache import get_cache
from app.config import settings
from app.db.models.proposal import (
    APPLIED_STATUSES,
    STATUS_APPLIED,
    STATUS_AUTO_APPLIED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_ROLLED_BACK,
    Proposal,
)

logger = logging.getLogger("company_brain.proposals.engine")


class ProposalError(Exception):
    """A proposal is invalid or cannot transition (bad payload, wrong status)."""


class ProposalHandler(Protocol):
    kind: str

    def validate(self, db: Session, payload: dict) -> None:
        """Raise ProposalError if the payload cannot be applied to the current graph."""

    def apply(self, db: Session, payload: dict) -> dict:
        """Mutate the graph; return a JSON-serializable rollback snapshot."""

    def rollback(self, db: Session, rollback_data: dict) -> None:
        """Restore the graph state captured by apply()."""


_HANDLERS: dict[str, ProposalHandler] = {}


def register_handler(cls):
    """Class decorator: register a handler under its `kind`."""
    _HANDLERS[cls.kind] = cls()
    return cls


def get_handler(kind: str) -> ProposalHandler:
    try:
        return _HANDLERS[kind]
    except KeyError:
        raise ProposalError(f"No handler registered for proposal kind '{kind}'.") from None


class ProposalEngine:
    def __init__(self, db: Session):
        self.db = db

    # -- lifecycle -----------------------------------------------------------
    def submit(
        self,
        kind: str,
        payload: dict,
        *,
        confidence: float,
        agent: str,
        evidence: dict | None = None,
        dedup_key: str | None = None,
    ) -> Proposal:
        """File a proposal. Returns the existing row if `dedup_key` was already
        filed (whatever its status — a rejected proposal stays rejected)."""
        get_handler(kind)  # fail fast on unknown kinds
        if dedup_key is not None:
            existing = self.db.scalar(
                select(Proposal).where(Proposal.kind == kind, Proposal.dedup_key == dedup_key)
            )
            if existing is not None:
                return existing

        proposal = Proposal(
            kind=kind,
            payload=payload,
            confidence=confidence,
            agent=agent,
            evidence=evidence,
            dedup_key=dedup_key,
        )
        self.db.add(proposal)
        self.db.flush()

        threshold = settings.proposal_auto_apply_threshold
        if threshold is not None and confidence >= threshold:
            self._apply(proposal, STATUS_AUTO_APPLIED)
        else:
            self.db.commit()
        return proposal

    def approve(self, proposal_id: uuid.UUID, *, reviewed_by: str) -> Proposal:
        proposal = self._get_in_status(proposal_id, STATUS_PENDING)
        proposal.reviewed_by = reviewed_by
        proposal.decided_at = func.now()
        self._apply(proposal, STATUS_APPLIED)
        return proposal

    def reject(self, proposal_id: uuid.UUID, *, reviewed_by: str) -> Proposal:
        proposal = self._get_in_status(proposal_id, STATUS_PENDING)
        proposal.status = STATUS_REJECTED
        proposal.reviewed_by = reviewed_by
        proposal.decided_at = func.now()
        self.db.commit()
        return proposal

    def rollback(self, proposal_id: uuid.UUID, *, reviewed_by: str) -> Proposal:
        proposal = self._get_in_status(proposal_id, *APPLIED_STATUSES)
        if proposal.rollback_data is None:
            raise ProposalError(f"Proposal {proposal_id} has no rollback snapshot.")
        get_handler(proposal.kind).rollback(self.db, proposal.rollback_data)
        proposal.status = STATUS_ROLLED_BACK
        proposal.reviewed_by = reviewed_by
        self.db.commit()
        get_cache().bump("graph")  # the applied change was visible — invalidate reads
        logger.info("Rolled back %s proposal %s", proposal.kind, proposal.id)
        return proposal

    # -- internals -------------------------------------------------------------
    def _get_in_status(self, proposal_id: uuid.UUID, *allowed: str) -> Proposal:
        proposal = self.db.get(Proposal, proposal_id)
        if proposal is None:
            raise ProposalError(f"Proposal {proposal_id} not found.")
        if proposal.status not in allowed:
            raise ProposalError(
                f"Proposal {proposal_id} is '{proposal.status}', expected {'/'.join(allowed)}."
            )
        return proposal

    def _apply(self, proposal: Proposal, final_status: str) -> None:
        handler = get_handler(proposal.kind)
        # Captured before mutating: after a mid-apply rollback the ORM instance
        # may be expired against a row that no longer exists.
        proposal_id = proposal.id
        fields = {
            "kind": proposal.kind,
            "payload": proposal.payload,
            "confidence": proposal.confidence,
            "agent": proposal.agent,
            "evidence": proposal.evidence,
            "dedup_key": proposal.dedup_key,
        }
        try:
            handler.validate(self.db, proposal.payload)
            proposal.rollback_data = handler.apply(self.db, proposal.payload)
            proposal.status = final_status
            proposal.applied_at = func.now()
            self.db.commit()
            get_cache().bump("graph")  # the graph changed — invalidate cached MCP reads
            logger.info("Applied %s proposal %s (%s)", proposal.kind, proposal.id, final_status)
        except Exception as exc:
            # Undo any partial graph mutation, then persist the failure itself.
            self.db.rollback()
            failed = self.db.get(Proposal, proposal_id)
            if failed is None:  # the submit itself was rolled back — re-file as failed
                failed = Proposal(**fields)
                self.db.add(failed)
            failed.status = STATUS_FAILED
            failed.error = str(exc)[:2000]
            self.db.commit()
            logger.warning("Proposal %s failed to apply: %s", proposal_id, exc)
            raise ProposalError(f"Proposal failed to apply: {exc}") from exc
