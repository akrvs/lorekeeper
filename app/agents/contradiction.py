"""Contradiction agent — finds scalar properties whose sources disagree.

After a merge the alias row keeps its original properties while the canonical
serves the winner's values. Where the same scalar key differs between an alias
and its canonical, two sources are asserting different facts about one entity
(GitHub says owner=alice, Slack says owner=bob). Each disagreement is filed as
a `fact_conflict` proposal with both claims in evidence; applying it marks the
property disputed rather than picking a side.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.agents.base import AgentFactory, MaintenanceAgent
from app.config import settings
from app.db.models import Node
from app.db.models.proposal import Proposal

logger = logging.getLogger("company_brain.agents.contradiction")

_SCALARS = (str, int, float, bool)
# System-stamped keys are bookkeeping, not source facts.
_SKIP_KEYS = {"stale", "stale_since", "importance", "disputed"}


@AgentFactory.register("contradiction")
class ContradictionAgent(MaintenanceAgent):
    def scan(self) -> list[Proposal]:
        filed: list[Proposal] = []
        duplicate = aliased(Node)
        pairs = self.db.execute(
            select(Node, duplicate)
            .join(duplicate, duplicate.canonical_node_id == Node.id)
            .where(Node.canonical_node_id.is_(None))
            .order_by(Node.name)
            .limit(settings.contradiction_scan_limit)
        ).all()
        for canonical, dup in pairs:
            disputed = (canonical.properties or {}).get("disputed") or {}
            for key, theirs in (dup.properties or {}).items():
                if key in _SKIP_KEYS or key in disputed or not isinstance(theirs, _SCALARS):
                    continue
                ours = (canonical.properties or {}).get(key)
                if not isinstance(ours, _SCALARS) or ours == theirs:
                    continue
                dedup_key = f"{canonical.id}:{key}"
                already = self.db.scalar(
                    select(func.count())
                    .select_from(Proposal)
                    .where(Proposal.kind == "fact_conflict", Proposal.dedup_key == dedup_key)
                )
                proposal = self.engine.submit(
                    "fact_conflict",
                    {
                        "node_id": str(canonical.id),
                        "property": key,
                        "values": [
                            {"value": ours, "source": canonical.source_system or "derived"},
                            {"value": theirs, "source": dup.source_system or "derived"},
                        ],
                        "reason": (
                            f"'{key}' on '{canonical.name}' disagrees across sources: "
                            f"{ours!r} vs {theirs!r}"
                        ),
                    },
                    # ponytail: flat confidence — the two values in evidence are
                    # what the reviewer judges, not a score.
                    confidence=0.6,
                    agent="contradiction",
                    evidence={
                        "canonical": {
                            "id": str(canonical.id),
                            "name": canonical.name,
                            "source": canonical.source_system,
                        },
                        "duplicate": {
                            "id": str(dup.id),
                            "name": dup.name,
                            "source": dup.source_system,
                        },
                        "property": key,
                    },
                    dedup_key=dedup_key,
                )
                if not already:
                    filed.append(proposal)
        logger.info("contradiction scan complete: %d proposal(s) filed", len(filed))
        return filed
