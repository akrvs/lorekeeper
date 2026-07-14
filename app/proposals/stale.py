"""stale_flag — mark a node whose evidence has gone quiet.

A knowledge graph that never forgets is a knowledge graph that lies: the
"payments oncall runbook" node extracted 14 months ago is still served with
confidence 1.0 today. The staleness agent files these proposals; applying one
stamps `stale: true` + `stale_since` into the node's JSONB properties, which
every MCP tool already surfaces to the querying agent — the citation is still
there, but the reader is warned the fact may have expired.

Deliberately a *flag*, not a deletion: stale facts are often the only record
of why something was decided. Rollback restores the exact prior properties.

payload: {"node_id": "<uuid>", "last_seen": "<iso date>", "reason": "..."}
"""

import uuid

from sqlalchemy.orm import Session

from app.db.models import Node
from app.proposals.engine import ProposalError, register_handler


def _get_node(db: Session, payload: dict) -> Node:
    try:
        node_id = uuid.UUID(str(payload["node_id"]))
    except (KeyError, ValueError) as exc:
        raise ProposalError(f"Malformed stale_flag payload: {exc}") from exc
    node = db.get(Node, node_id)
    if node is None:
        raise ProposalError(f"Node {payload['node_id']} does not exist.")
    return node


@register_handler
class StaleFlagHandler:
    kind = "stale_flag"

    def validate(self, db: Session, payload: dict) -> None:
        node = _get_node(db, payload)
        if node.canonical_node_id is not None:
            raise ProposalError(f"Node {node.id} is a merged alias; flag its canonical instead.")
        if node.properties.get("stale"):
            raise ProposalError(f"Node {node.id} is already flagged stale.")

    def apply(self, db: Session, payload: dict) -> dict:
        node = _get_node(db, payload)
        before = dict(node.properties)
        node.properties = {
            **node.properties,
            "stale": True,
            "stale_since": payload.get("last_seen"),
        }
        db.flush()
        return {"node_id": str(node.id), "properties_before": before}

    def rollback(self, db: Session, rollback_data: dict) -> None:
        node = db.get(Node, uuid.UUID(rollback_data["node_id"]))
        if node is None:
            raise ProposalError("Cannot roll back: the flagged node no longer exists.")
        node.properties = rollback_data["properties_before"]
        db.flush()
