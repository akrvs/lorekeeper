"""edge_add — assert a missing relationship between two existing nodes.

The edge suggester finds embedding-close, well-evidenced node pairs with no
edge and asks the LLM to name the relationship from the live registry (or
abstain). Applying inserts the typed edge; rollback deletes it exactly.

payload: {"source_id": "<uuid>", "target_id": "<uuid>", "relationship": "...",
          "confidence": 0.0-1.0, "reason": "..."}
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Edge, Node
from app.db.models.ontology import OntologyRelationshipType
from app.proposals.engine import ProposalError, register_handler


def _parse(db: Session, payload: dict) -> tuple[Node, Node, str]:
    try:
        source_id = uuid.UUID(str(payload["source_id"]))
        target_id = uuid.UUID(str(payload["target_id"]))
        relationship = payload["relationship"]
    except (KeyError, ValueError) as exc:
        raise ProposalError(f"Malformed edge_add payload: {exc}") from exc
    source, target = db.get(Node, source_id), db.get(Node, target_id)
    if source is None or target is None:
        missing = source_id if source is None else target_id
        raise ProposalError(f"Node {missing} does not exist.")
    return source, target, relationship


@register_handler
class EdgeAddHandler:
    kind = "edge_add"

    def validate(self, db: Session, payload: dict) -> None:
        source, target, relationship = _parse(db, payload)
        if source.id == target.id:
            raise ProposalError("Cannot add a self-edge.")
        for node, role in ((source, "source"), (target, "target")):
            if node.canonical_node_id is not None:
                raise ProposalError(f"The {role} node {node.id} is a merged alias.")
        if db.get(OntologyRelationshipType, relationship) is None:
            raise ProposalError(f"Unknown relationship type '{relationship}'.")
        existing = db.scalar(
            select(Edge.id).where(
                Edge.source_id == source.id,
                Edge.target_id == target.id,
                Edge.relationship_type == relationship,
            )
        )
        if existing is not None:
            raise ProposalError("The edge already exists.")

    def apply(self, db: Session, payload: dict) -> dict:
        source, target, relationship = _parse(db, payload)
        edge = Edge(
            source_id=source.id,
            target_id=target.id,
            relationship_type=relationship,
            properties={},
            confidence=float(payload.get("confidence", 0.5)),
        )
        db.add(edge)
        db.flush()
        return {"edge_id": str(edge.id)}

    def rollback(self, db: Session, rollback_data: dict) -> None:
        edge = db.get(Edge, uuid.UUID(rollback_data["edge_id"]))
        if edge is None:
            raise ProposalError("Cannot roll back: the suggested edge no longer exists.")
        db.delete(edge)
        db.flush()
