"""Digest — a human-readable summary of recent graph activity.

Read-only by design: it files no proposals and writes nothing, so it is not
registered with the AgentFactory. Run it via

    python -m app.agents.run digest [--days N]

or on the scheduler's interval.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Node
from app.db.models.proposal import APPLIED_STATUSES, Proposal
from app.db.models.source import IngestionRun, RawDocument


def build_digest(db: Session, days: int = 7) -> str:
    since = datetime.now(UTC) - timedelta(days=days)
    lines = [f"# Lorekeeper digest — last {days} day(s)", ""]

    lines.append("## Ingestion")
    runs = db.execute(
        select(IngestionRun.source_system, IngestionRun.status, func.count())
        .where(IngestionRun.started_at >= since)
        .group_by(IngestionRun.source_system, IngestionRun.status)
        .order_by(IngestionRun.source_system)
    ).all()
    lines += [f"- {src}: {n} run(s) {status}" for src, status, n in runs] or ["- (no runs)"]
    documents = db.scalar(
        select(func.count()).select_from(RawDocument).where(RawDocument.ingested_at >= since)
    )
    lines.append(f"- documents ingested: {documents}")

    lines += ["", "## Graph growth"]
    nodes = db.scalar(
        select(func.count())
        .select_from(Node)
        .where(Node.created_at >= since, Node.canonical_node_id.is_(None))
    )
    lines.append(f"- entities created: {nodes}")

    lines += ["", "## Self-maintenance"]
    filed = db.execute(
        select(Proposal.kind, func.count())
        .where(Proposal.created_at >= since)
        .group_by(Proposal.kind)
        .order_by(Proposal.kind)
    ).all()
    lines += [f"- filed {n} {kind} proposal(s)" for kind, n in filed] or ["- (nothing filed)"]
    applied = db.execute(
        select(Proposal.kind, func.count())
        .where(Proposal.status.in_(APPLIED_STATUSES), Proposal.applied_at >= since)
        .group_by(Proposal.kind)
        .order_by(Proposal.kind)
    ).all()
    lines += [f"- applied {n} {kind} proposal(s)" for kind, n in applied]
    rejected = db.scalar(
        select(func.count())
        .select_from(Proposal)
        .where(Proposal.status == "rejected", Proposal.decided_at >= since)
    )
    if rejected:
        lines.append(f"- rejected {rejected} proposal(s)")
    pending = db.scalar(
        select(func.count()).select_from(Proposal).where(Proposal.status == "pending")
    )
    lines.append(f"- pending review now: {pending}")
    return "\n".join(lines)
