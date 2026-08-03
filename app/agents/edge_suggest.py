"""Edge suggester — link prediction with the LLM naming the relationship.

Candidate pairs are embedding-close, both well-evidenced (mention floor), and
share no edge in either direction. Similarity alone cannot name a typed
relationship, so each new pair goes to the LLM, which must pick a term from
the live ontology registry or abstain — an abstention files nothing. The
offline stub abstains by construction, so LLM_PROVIDER=stub keeps this agent
silent instead of guessing. Suggestions land as `edge_add` proposals; the
human veto is the same queue as every other change.
"""

import json
import logging
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import aliased

from app.agents.base import AgentFactory, MaintenanceAgent
from app.config import settings
from app.db.models import Edge, Node, NodeMention
from app.db.models.ontology import OntologyRelationshipType
from app.db.models.proposal import Proposal
from app.llm import get_llm_provider

logger = logging.getLogger("company_brain.agents.edge_suggest")

# Below this judge confidence a suggestion is treated as an abstention.
_MIN_JUDGE_CONFIDENCE = 0.5

_JUDGE_PROMPT = (
    "You suggest missing relationships in an organizational knowledge graph. "
    "Two nodes are semantically close and well-evidenced but share no edge. "
    "If a DIRECTED relationship from the allowed list clearly holds between "
    "them, name it and give its direction; otherwise abstain by returning an "
    "empty relationship. Only use relationships from the allowed list. "
    "confidence is how sure YOU are (0-1); rationale is ONE sentence a human "
    "reviewer will read."
)


class EdgeSuggestion(BaseModel):
    """LLM suggestion for one pair. Defaults are the abstention the offline
    stub returns (empty relationship files nothing)."""

    relationship: str = ""
    direction: Literal["a_to_b", "b_to_a"] = "a_to_b"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""


@AgentFactory.register("edge_suggest")
class EdgeSuggestAgent(MaintenanceAgent):
    def scan(self) -> list[Proposal]:
        try:
            judge = get_llm_provider()
        except Exception as exc:  # noqa: BLE001 — no provider, no suggestions
            logger.warning("edge_suggest: LLM unavailable (%s); nothing filed", exc)
            return []
        allowed = set(self.db.scalars(select(OntologyRelationshipType.name)).all())
        filed: list[Proposal] = []
        for a_id, b_id, dist in self._candidates():
            node_a, node_b = self.db.get(Node, a_id), self.db.get(Node, b_id)
            if node_a is None or node_b is None:
                continue
            dedup_key = ":".join(sorted((str(a_id), str(b_id))))
            if self._already_filed(dedup_key):
                continue
            suggestion = self._judge(judge, node_a, node_b, allowed, dist)
            if (
                suggestion is None
                or not suggestion.relationship
                or suggestion.relationship not in allowed
                or suggestion.confidence < _MIN_JUDGE_CONFIDENCE
            ):
                continue
            source, target = (
                (node_a, node_b) if suggestion.direction == "a_to_b" else (node_b, node_a)
            )
            proposal = self.engine.submit(
                "edge_add",
                {
                    "source_id": str(source.id),
                    "target_id": str(target.id),
                    "relationship": suggestion.relationship,
                    "confidence": round(suggestion.confidence, 3),
                    "reason": (
                        f"edge_suggest: '{source.name}' -[{suggestion.relationship}]-> "
                        f"'{target.name}'"
                    ),
                },
                confidence=round(suggestion.confidence, 3),
                agent="edge_suggest",
                evidence={
                    "cosine_distance": round(float(dist), 4),
                    "source": {"id": str(source.id), "name": source.name},
                    "target": {"id": str(target.id), "name": target.name},
                    "llm_rationale": suggestion.rationale,
                },
                dedup_key=dedup_key,
            )
            filed.append(proposal)
        logger.info("edge_suggest scan complete: %d proposal(s) filed", len(filed))
        return filed

    def _candidates(self):
        a, b = aliased(Node, name="a"), aliased(Node, name="b")
        vec_dist = a.embedding.cosine_distance(b.embedding)
        a_mentions = (
            select(func.count())
            .select_from(NodeMention)
            .where(NodeMention.node_id == a.id)
            .scalar_subquery()
        )
        b_mentions = (
            select(func.count())
            .select_from(NodeMention)
            .where(NodeMention.node_id == b.id)
            .scalar_subquery()
        )
        edge_exists = (
            select(Edge.id)
            .where(
                or_(
                    and_(Edge.source_id == a.id, Edge.target_id == b.id),
                    and_(Edge.source_id == b.id, Edge.target_id == a.id),
                )
            )
            .exists()
        )
        stmt = (
            select(a.id, b.id, vec_dist)
            .where(
                a.id < b.id,  # each unordered pair once
                a.canonical_node_id.is_(None),
                b.canonical_node_id.is_(None),
                a.embedding.is_not(None),
                b.embedding.is_not(None),
                vec_dist <= settings.edge_suggest_vec_threshold,
                a_mentions >= settings.edge_suggest_min_mentions,
                b_mentions >= settings.edge_suggest_min_mentions,
                not_(edge_exists),
            )
            .order_by(vec_dist)
            .limit(settings.edge_suggest_scan_limit)
        )
        return self.db.execute(stmt).all()

    def _judge(
        self, judge, node_a: Node, node_b: Node, allowed: set[str], dist: float
    ) -> EdgeSuggestion | None:
        content = (
            f"{self._render('ENTITY A', node_a)}\n\n"
            f"{self._render('ENTITY B', node_b)}\n\n"
            f"embedding cosine distance: {float(dist):.2f}\n"
            f"allowed relationships: {', '.join(sorted(allowed))}"
        )
        try:
            return judge.extract(_JUDGE_PROMPT, content, EdgeSuggestion)
        except Exception as exc:  # noqa: BLE001 — a judge outage must not kill the scan
            logger.warning("edge_suggest: judge failed for %s/%s: %s", node_a.id, node_b.id, exc)
            return None

    @staticmethod
    def _render(label: str, node: Node) -> str:
        lines = [f"{label}:", f"  type: {node.node_type}", f"  name: {node.name}"]
        if node.summary:
            lines.append(f"  summary: {node.summary[:500]}")
        if node.properties:
            lines.append(f"  properties: {json.dumps(node.properties)[:500]}")
        return "\n".join(lines)

    def _already_filed(self, dedup_key: str) -> bool:
        count = self.db.scalar(
            select(func.count())
            .select_from(Proposal)
            .where(Proposal.kind == "edge_add", Proposal.dedup_key == dedup_key)
        )
        return count > 0
