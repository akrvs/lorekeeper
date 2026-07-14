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

Similarity scores catch candidates; they can't judge identity ('payments-service'
vs 'payments-db' score high and are different things). So each NEW pair is also
put to the configured LLM for a same/different/unsure verdict
(DEDUP_LLM_JUDGE, on by default). The verdict lands in the proposal's evidence
for the reviewer, folds into the confidence score (which orders the queue), and
a confidently-"different" verdict stops the pair from being filed at all.
Already-filed pairs are never re-judged, and the offline stub provider returns
"unsure" — no cost, no behavior change. A judge failure degrades to filing the
unjudged heuristic proposal: the queue must not go dark because the LLM did.

Winner selection (deterministic): sourced beats derived (it has a natural key
future ingests will keep hitting), then more mentions, then the older node.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import aliased

from app.agents.base import AgentFactory, MaintenanceAgent
from app.config import settings
from app.db.models import Node, NodeMention
from app.db.models.proposal import Proposal
from app.llm import get_llm_provider
from app.proposals import merge_dedup_key

logger = logging.getLogger("company_brain.agents.dedup")

_JUDGE_PROMPT = (
    "You judge merge candidates in an organizational knowledge graph. Two nodes "
    "of the same type were flagged as possible duplicates by lexical/semantic "
    "similarity. Decide whether they refer to the SAME real-world entity.\n"
    "- verdict 'same': one entity observed twice (e.g. a person's GitHub and "
    "Slack accounts, a doc and its wikilink stub).\n"
    "- verdict 'different': related or similarly named, but distinct things "
    "(e.g. 'payments-service' vs 'payments-db', two teammates sharing a first "
    "name).\n"
    "- verdict 'unsure': the evidence cannot settle it.\n"
    "confidence is how sure YOU are of that verdict (0-1). rationale is ONE "
    "sentence a human reviewer will read next to the scores. Judge identity, "
    "not similarity — the similarity scores are already known."
)


class MergeJudgment(BaseModel):
    """LLM verdict on one candidate pair. Defaults are the no-opinion case the
    offline stub returns (`unsure` at zero confidence changes nothing)."""

    verdict: Literal["same", "different", "unsure"] = "unsure"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""


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
        try:
            judge = get_llm_provider() if settings.dedup_llm_judge else None
        except Exception as exc:  # noqa: BLE001 — misconfigured provider ≠ no dedup
            logger.warning("dedup: LLM judge unavailable (%s); filing unjudged proposals", exc)
            judge = None
        filed: list[Proposal] = []
        for cand in self._candidates():
            node_a, node_b = self.db.get(Node, cand.a_id), self.db.get(Node, cand.b_id)
            if node_a is None or node_b is None:
                continue
            dedup_key = merge_dedup_key(node_a.id, node_b.id)
            if self._already_filed(dedup_key):
                continue  # earlier run's decision stands; don't re-judge, don't re-file
            judgment = self._judge(judge, cand, node_a, node_b)
            if (
                judgment is not None
                and judgment.verdict == "different"
                and judgment.confidence >= settings.dedup_llm_skip_threshold
            ):
                logger.info(
                    "dedup: LLM ruled out '%s' / '%s': %s",
                    node_a.name,
                    node_b.name,
                    judgment.rationale,
                )
                continue
            proposal = self._file(cand, node_a, node_b, judgment, dedup_key)
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

    # -- LLM judgment ------------------------------------------------------------
    def _judge(self, judge, cand: Candidate, node_a: Node, node_b: Node) -> MergeJudgment | None:
        if judge is None:
            return None
        signals = []
        if cand.name_sim is not None:
            signals.append(f"name similarity {cand.name_sim:.2f}")
        if cand.vec_dist is not None:
            signals.append(f"embedding cosine distance {cand.vec_dist:.2f}")
        content = (
            f"{self._render('ENTITY A', node_a)}\n\n"
            f"{self._render('ENTITY B', node_b)}\n\n"
            f"similarity signals: {', '.join(signals) or 'none'}"
        )
        try:
            return judge.extract(_JUDGE_PROMPT, content, MergeJudgment)
        except Exception as exc:  # noqa: BLE001 — a judge outage must not kill the scan
            logger.warning("dedup: LLM judge failed for %s/%s: %s", node_a.id, node_b.id, exc)
            return None

    @staticmethod
    def _render(label: str, node: Node) -> str:
        origin = (
            f"{node.source_system} (external id: {node.external_id})"
            if node.external_id is not None
            else "derived by extraction (no source id)"
        )
        lines = [f"{label}:", f"  type: {node.node_type}", f"  name: {node.name}"]
        if node.summary:
            lines.append(f"  summary: {node.summary[:500]}")
        if node.properties:
            lines.append(f"  properties: {json.dumps(node.properties)[:500]}")
        lines.append(f"  origin: {origin}")
        return "\n".join(lines)

    # -- proposal filing ---------------------------------------------------------
    def _already_filed(self, dedup_key: str) -> bool:
        count = self.db.scalar(
            select(func.count()).select_from(Proposal).where(Proposal.dedup_key == dedup_key)
        )
        return count > 0

    def _file(
        self,
        cand: Candidate,
        node_a: Node,
        node_b: Node,
        judgment: MergeJudgment | None,
        dedup_key: str,
    ) -> Proposal | None:
        winner, loser = self._pick_winner(node_a, node_b)
        evidence = {
            "name_similarity": cand.name_sim,
            "cosine_distance": cand.vec_dist,
            "winner": {"name": winner.name, "source": winner.source_system or "derived"},
            "loser": {"name": loser.name, "source": loser.source_system or "derived"},
        }
        if judgment is not None and (judgment.verdict != "unsure" or judgment.rationale):
            evidence["llm_judgment"] = judgment.model_dump()
        try:
            return self.engine.submit(
                "entity_merge",
                {
                    "loser_id": str(loser.id),
                    "winner_id": str(winner.id),
                    "reason": f"dedup: '{loser.name}' looks like a duplicate of '{winner.name}'",
                },
                confidence=self._confidence(cand, judgment),
                agent="dedup",
                evidence=evidence,
                dedup_key=dedup_key,
            )
        except Exception as exc:  # noqa: BLE001 — one bad pair must not kill the scan
            logger.warning("dedup: submit failed for %s/%s: %s", node_a.id, node_b.id, exc)
            return None

    @staticmethod
    def _confidence(cand: Candidate, judgment: MergeJudgment | None) -> float:
        """Fold the judge's verdict into the heuristic score: a confident 'same'
        can only raise it, a (non-skipping) 'different' can only lower it, and
        'unsure' / no judge leaves the similarity blend untouched."""
        if judgment is None or judgment.verdict == "unsure":
            return cand.confidence
        if judgment.verdict == "same":
            return max(cand.confidence, round(judgment.confidence, 3))
        return min(cand.confidence, round(1.0 - judgment.confidence, 3))

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
