import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Edge, Node, NodeMention
from app.proposals.engine import ProposalError, register_handler


def _parse_id(payload: dict, key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload[key]))
    except (KeyError, ValueError) as exc:
        raise ProposalError(f"Malformed payload: {exc}") from exc


def _assert_orphan(db: Session, node: Node) -> None:
    degree = db.scalar(
        select(func.count())
        .select_from(Edge)
        .where(or_(Edge.source_id == node.id, Edge.target_id == node.id))
    )
    if degree:
        raise ProposalError(f"Node {node.id} has {degree} edge(s); not an orphan.")
    mentions = db.scalar(
        select(func.count()).select_from(NodeMention).where(NodeMention.node_id == node.id)
    )
    if mentions:
        raise ProposalError(f"Node {node.id} has {mentions} mention(s); not an orphan.")
    aliases = db.scalar(
        select(func.count()).select_from(Node).where(Node.canonical_node_id == node.id)
    )
    if aliases:
        raise ProposalError(f"Node {node.id} is the canonical target of {aliases} alias(es).")


@register_handler
class NodeRemovalHandler:
    kind = "node_removal"

    def validate(self, db: Session, payload: dict) -> None:
        node = db.get(Node, _parse_id(payload, "node_id"))
        if node is None:
            raise ProposalError(f"Node {payload['node_id']} does not exist.")
        _assert_orphan(db, node)

    def apply(self, db: Session, payload: dict) -> dict:
        node = db.get(Node, _parse_id(payload, "node_id"))
        snapshot = {
            "id": str(node.id),
            "node_type": node.node_type,
            "name": node.name,
            "summary": node.summary,
            "properties": node.properties,
            "embedding": [float(v) for v in node.embedding] if node.embedding is not None else None,
            "source_system": node.source_system,
            "external_id": node.external_id,
            "confidence": node.confidence,
        }
        db.delete(node)
        db.flush()
        return {"node": snapshot}

    def rollback(self, db: Session, rollback_data: dict) -> None:
        row = dict(rollback_data["node"])
        row["id"] = uuid.UUID(row["id"])
        db.add(Node(**row))
        db.flush()


@register_handler
class EdgeRemovalHandler:
    kind = "edge_removal"

    def validate(self, db: Session, payload: dict) -> None:
        if db.get(Edge, _parse_id(payload, "edge_id")) is None:
            raise ProposalError(f"Edge {payload['edge_id']} does not exist.")

    def apply(self, db: Session, payload: dict) -> dict:
        edge = db.get(Edge, _parse_id(payload, "edge_id"))
        snapshot = {
            "id": str(edge.id),
            "source_id": str(edge.source_id),
            "target_id": str(edge.target_id),
            "relationship_type": edge.relationship_type,
            "properties": edge.properties,
            "weight": edge.weight,
            "confidence": edge.confidence,
            "evidence_document_id": (
                str(edge.evidence_document_id) if edge.evidence_document_id else None
            ),
        }
        db.delete(edge)
        db.flush()
        return {"edge": snapshot}

    def rollback(self, db: Session, rollback_data: dict) -> None:
        row = dict(rollback_data["edge"])
        row["id"] = uuid.UUID(row["id"])
        row["source_id"] = uuid.UUID(row["source_id"])
        row["target_id"] = uuid.UUID(row["target_id"])
        if row["evidence_document_id"]:
            row["evidence_document_id"] = uuid.UUID(row["evidence_document_id"])
        db.add(Edge(**row))
        db.flush()
