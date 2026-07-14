"""Staleness agent — finds nodes whose evidence trail has gone cold.

A node's freshness is the newest document that mentions it (source_updated_at,
falling back to ingestion time). Nodes last evidenced more than
`STALE_AFTER_DAYS` ago get a `stale_flag` proposal; confidence grows with age
(barely-over-the-line facts are debatable, two-year-old facts are not).

Nodes with no mentions at all are skipped — that's a provenance gap for the
dedup/merge machinery, not staleness. Already-flagged nodes are skipped. The
dedup key includes the last-seen date, so a node that is refreshed by new
evidence and later goes quiet again can be re-flagged, while re-scans of the
same quiet node stay idempotent.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.agents.base import AgentFactory, MaintenanceAgent
from app.config import settings
from app.db.models import Node, NodeMention, RawDocument
from app.db.models.proposal import Proposal

logger = logging.getLogger("company_brain.agents.staleness")


@AgentFactory.register("staleness")
class StalenessAgent(MaintenanceAgent):
    def scan(self) -> list[Proposal]:
        filed: list[Proposal] = []
        now = datetime.now(UTC)
        for node_id, name, last_seen in self._quiet_nodes():
            age_days = (now - last_seen).days
            dedup_key = f"{node_id}:{last_seen.date().isoformat()}"
            already = self.db.scalar(
                select(func.count())
                .select_from(Proposal)
                .where(Proposal.kind == "stale_flag", Proposal.dedup_key == dedup_key)
            )
            proposal = self.engine.submit(
                "stale_flag",
                {
                    "node_id": str(node_id),
                    "last_seen": last_seen.date().isoformat(),
                    "reason": f"no evidence for '{name}' in {age_days} days",
                },
                confidence=self._confidence(age_days),
                agent="staleness",
                evidence={"last_seen": last_seen.isoformat(), "age_days": age_days},
                dedup_key=dedup_key,
            )
            if not already:
                filed.append(proposal)
        logger.info("staleness scan complete: %d proposal(s) filed", len(filed))
        return filed

    def _quiet_nodes(self):
        freshness = func.max(
            func.coalesce(
                RawDocument.source_updated_at, RawDocument.ingested_at, RawDocument.created_at
            )
        )
        cutoff = func.now() - func.make_interval(0, 0, 0, settings.stale_after_days)
        stmt = (
            select(Node.id, Node.name, freshness.label("last_seen"))
            .join(NodeMention, NodeMention.node_id == Node.id)
            .join(RawDocument, RawDocument.id == NodeMention.document_id)
            .where(
                Node.canonical_node_id.is_(None),
                # JSONB guard: skip nodes already carrying a truthy stale flag.
                func.coalesce(Node.properties["stale"].astext, "false") != "true",
            )
            .group_by(Node.id, Node.name)
            .having(freshness < cutoff)
            .order_by(freshness)
            .limit(settings.stale_scan_limit)
        )
        return self.db.execute(stmt).all()

    @staticmethod
    def _confidence(age_days: int) -> float:
        """0.5 at the threshold, +0.1 per extra half-threshold, capped at 0.9."""
        over = max(0, age_days - settings.stale_after_days)
        return round(min(0.9, 0.5 + 0.1 * (over / (settings.stale_after_days / 2))), 3)
