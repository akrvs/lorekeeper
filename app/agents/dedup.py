"""Dedup agent — finds duplicate entities the insert-time resolver can't catch.

The resolver auto-merges *derived* nodes at insert time when they are nearly
identical (name similarity ≥ 0.55 OR cosine distance ≤ 0.15). Two whole classes
of duplicates slip past it:

  1. **Cross-source sourced entities** — GitHub user `alice` and Slack user
     `alice` each have an external_id, so the resolver upserts them separately
     and never compares them.
  2. **Near-misses** — derived pairs in the gray band below the auto-merge
     thresholds that are still probably the same thing.

This agent scans for both and files `entity_merge` proposals with the evidence
(scores, mention counts) a reviewer needs. Pairs where BOTH nodes are sourced
from the SAME system are skipped: two distinct GitHub ids are two distinct
accounts, however similar their names.

Winner selection (deterministic): sourced beats derived (it has a natural key
future ingests will keep hitting), then more mentions, then the older node.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import aliased

from app.agents.base import AgentFactory, MaintenanceAgent
from app.config import settings
from app.db.models import Node, NodeMention
from app.db.models.proposal import Proposal
from app.proposals import merge_dedup_key

logger = logging.getLogger("company_brain.agents.dedup")


@dataclass
class Candidate:
    a_id: uuid.UUID
    b_id: uuid.UUID
    name_sim: float | None
    vec_dist: float | None

    @property
    def confidence(self) -> float:
        """Blend lexical + semantic agreement into one reviewer-facing score.
        Both signals present: weighted blend (meaning counts more than spelling);
        one signal: use it alone."""
        sem = None if self.vec_dist is None else max(0.0, 1.0 - self.vec_dist)
        if self.name_sim is not None and sem is not None:
            return round(0.4 * self.name_sim + 0.6 * sem, 3)
        return round(sem if sem is not None else (self.name_sim or 0.0), 3)


@AgentFactory.register("dedup")
class DedupAgent(MaintenanceAgent):
    def scan(self) -> list[Proposal]:
        filed: list[Proposal] = []
        for cand in self._candidates():
            proposal = self._file(cand)
            if proposal is not None:
                filed.append(proposal)
        logger.info("dedup scan complete: %d proposal(s) filed", len(filed))
        return filed

    # -- candidate discovery ---------------------------------------------------
    def _candidates(self) -> list[Candidate]:
        a, b = aliased(Node, name="a"), aliased(Node, name="b")
        name_sim = func.similarity(a.name, b.name)
        vec_dist = a.embedding.cosine_distance(b.embedding)

        lexical = name_sim >= settings.dedup_name_threshold
        semantic = and_(
            a.embedding.is_not(None),
            b.embedding.is_not(None),
            vec_dist <= settings.dedup_vec_threshold,
        )
        both_same_source = and_(
            a.external_id.is_not(None),
            b.external_id.is_not(None),
            a.source_system == b.source_system,
        )

        stmt = (
            select(
                a.id,
                b.id,
                name_sim,
                vec_dist,
            )
            .where(
                a.node_type == b.node_type,
                a.id < b.id,  # each unordered pair once
                a.canonical_node_id.is_(None),
                b.canonical_node_id.is_(None),
                not_(both_same_source),
                or_(lexical, semantic),
            )
            .order_by(name_sim.desc().nulls_last())
            .limit(settings.dedup_scan_limit)
        )
        return [
            Candidate(
                a_id=row[0],
                b_id=row[1],
                name_sim=float(row[2]) if row[2] is not None else None,
                vec_dist=float(row[3]) if row[3] is not None else None,
            )
            for row in self.db.execute(stmt).all()
        ]

    # -- proposal filing ---------------------------------------------------------
    def _file(self, cand: Candidate) -> Proposal | None:
        node_a = self.db.get(Node, cand.a_id)
        node_b = self.db.get(Node, cand.b_id)
        if node_a is None or node_b is None:
            return None
        winner, loser = self._pick_winner(node_a, node_b)

        dedup_key = merge_dedup_key(node_a.id, node_b.id)
        before = self.db.scalar(
            select(func.count()).select_from(Proposal).where(Proposal.dedup_key == dedup_key)
        )
        try:
            proposal = self._submit(cand, winner, loser, dedup_key)
        except Exception as exc:  # noqa: BLE001 — one bad pair must not kill the scan
            logger.warning("dedup: submit failed for %s/%s: %s", node_a.id, node_b.id, exc)
            return None
        return proposal if before == 0 else None  # None: already filed by an earlier run

    def _submit(self, cand: Candidate, winner: Node, loser: Node, dedup_key: str) -> Proposal:
        return self.engine.submit(
            "entity_merge",
            {
                "loser_id": str(loser.id),
                "winner_id": str(winner.id),
                "reason": f"dedup: '{loser.name}' looks like a duplicate of '{winner.name}'",
            },
            confidence=cand.confidence,
            agent="dedup",
            evidence={
                "name_similarity": cand.name_sim,
                "cosine_distance": cand.vec_dist,
                "winner": {"name": winner.name, "source": winner.source_system or "derived"},
                "loser": {"name": loser.name, "source": loser.source_system or "derived"},
            },
            dedup_key=dedup_key,
        )

    def _pick_winner(self, a: Node, b: Node) -> tuple[Node, Node]:
        a_sourced, b_sourced = a.external_id is not None, b.external_id is not None
        if a_sourced != b_sourced:
            return (a, b) if a_sourced else (b, a)
        a_mentions, b_mentions = self._mentions(a.id), self._mentions(b.id)
        if a_mentions != b_mentions:
            return (a, b) if a_mentions > b_mentions else (b, a)
        return (a, b) if a.created_at <= b.created_at else (b, a)

    def _mentions(self, node_id: uuid.UUID) -> int:
        return self.db.scalar(
            select(func.count()).select_from(NodeMention).where(NodeMention.node_id == node_id)
        )
