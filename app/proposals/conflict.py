"""fact_conflict — mark a property whose sources disagree.

Cross-source duplicates keep their original property bags even after a merge
(the alias row survives, winner's keys win). When the same scalar property
holds different values on the two rows, the graph is silently serving one
source's claim over another's. Applying this proposal stamps the disagreement
into the canonical node's `disputed` properties, so every MCP answer shows
both claims. No winner is picked — humans or fresh evidence resolve it.

payload: {"node_id": "<uuid>", "property": "...", "values": [...], "reason": "..."}
"""

import uuid

from sqlalchemy.orm import Session

from app.db.models import Node
from app.proposals.engine import ProposalError, register_handler


def _get_node(db: Session, payload: dict) -> tuple[Node, str]:
    try:
        node_id = uuid.UUID(str(payload["node_id"]))
        key = payload["property"]
    except (KeyError, ValueError) as exc:
        raise ProposalError(f"Malformed fact_conflict payload: {exc}") from exc
    node = db.get(Node, node_id)
    if node is None:
        raise ProposalError(f"Node {payload['node_id']} does not exist.")
    return node, key


@register_handler
class FactConflictHandler:
    kind = "fact_conflict"

    def validate(self, db: Session, payload: dict) -> None:
        node, key = _get_node(db, payload)
        if node.canonical_node_id is not None:
            raise ProposalError(f"Node {node.id} is a merged alias; dispute its canonical instead.")
        if key in (node.properties.get("disputed") or {}):
            raise ProposalError(f"'{key}' on node {node.id} is already disputed.")
        if not payload.get("values"):
            raise ProposalError("fact_conflict payload carries no values.")

    def apply(self, db: Session, payload: dict) -> dict:
        node, key = _get_node(db, payload)
        before = dict(node.properties)
        disputed = dict(node.properties.get("disputed") or {})
        disputed[key] = payload["values"]
        node.properties = {**node.properties, "disputed": disputed}
        db.flush()
        return {"node_id": str(node.id), "properties_before": before}

    def rollback(self, db: Session, rollback_data: dict) -> None:
        node = db.get(Node, uuid.UUID(rollback_data["node_id"]))
        if node is None:
            raise ProposalError("Cannot roll back: the disputed node no longer exists.")
        node.properties = rollback_data["properties_before"]
        db.flush()
