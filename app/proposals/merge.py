"""entity_merge — fold a duplicate node (the loser) into its canonical twin.

The merge is *soft*: the loser row survives with `canonical_node_id` pointing at
the winner, so the resolver keeps matching future mentions of either name and
following the chain (`Resolver._canonical_id`) to the survivor. Everything else
moves: edges and mentions are repointed to the winner (folding into existing
winner rows where the unique constraints collide), and the winner absorbs the
loser's properties/summary/confidence.

apply() returns a row-level snapshot of every mutation, so rollback() restores
the exact prior graph — including edges that were folded or dropped.

payload:       {"loser_id": "<uuid>", "winner_id": "<uuid>", "reason": "..."}
rollback_data: see _apply — winner_before / repointed / folded / deleted rows.
"""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Edge, Node, NodeMention
from app.proposals.engine import ProposalError, register_handler


def _edge_row(e: Edge) -> dict:
    return {
        "id": str(e.id),
        "source_id": str(e.source_id),
        "target_id": str(e.target_id),
        "relationship_type": e.relationship_type,
        "properties": e.properties,
        "weight": e.weight,
        "confidence": e.confidence,
        "evidence_document_id": str(e.evidence_document_id) if e.evidence_document_id else None,
    }


def _mention_row(m: NodeMention) -> dict:
    return {
        "id": str(m.id),
        "node_id": str(m.node_id),
        "document_id": str(m.document_id),
        "context": m.context,
    }


@register_handler
class EntityMergeHandler:
    kind = "entity_merge"

    # -- validate --------------------------------------------------------------
    def validate(self, db: Session, payload: dict) -> None:
        loser, winner = self._nodes(db, payload)
        if loser.id == winner.id:
            raise ProposalError("Cannot merge a node into itself.")
        if loser.node_type != winner.node_type:
            raise ProposalError(
                f"Cannot merge across node types ({loser.node_type} -> {winner.node_type})."
            )
        for node, role in ((loser, "loser"), (winner, "winner")):
            if node.canonical_node_id is not None:
                raise ProposalError(f"The {role} node {node.id} is already merged elsewhere.")

    # -- apply -------------------------------------------------------------------
    def apply(self, db: Session, payload: dict) -> dict:
        loser, winner = self._nodes(db, payload)

        repointed_edges: list[dict] = []
        folded_edges: list[dict] = []
        deleted_edges: list[dict] = []
        loser_edges = db.scalars(
            select(Edge).where(or_(Edge.source_id == loser.id, Edge.target_id == loser.id))
        ).all()
        for e in loser_edges:
            new_src = winner.id if e.source_id == loser.id else e.source_id
            new_tgt = winner.id if e.target_id == loser.id else e.target_id
            if new_src == new_tgt:
                # An edge between the two duplicates collapses to a self-loop.
                deleted_edges.append(_edge_row(e))
                db.delete(e)
                db.flush()
                continue
            twin = db.scalar(
                select(Edge).where(
                    Edge.source_id == new_src,
                    Edge.target_id == new_tgt,
                    Edge.relationship_type == e.relationship_type,
                )
            )
            if twin is not None:
                # The winner already asserts this fact — fold, as re-ingestion would.
                folded_edges.append(
                    {
                        "id": str(twin.id),
                        "weight": twin.weight,
                        "confidence": twin.confidence,
                        "properties": twin.properties,
                    }
                )
                twin.weight += e.weight
                twin.confidence = max(twin.confidence, e.confidence)
                twin.properties = {**e.properties, **twin.properties}
                deleted_edges.append(_edge_row(e))
                db.delete(e)
            else:
                repointed_edges.append(
                    {"id": str(e.id), "source_id": str(e.source_id), "target_id": str(e.target_id)}
                )
                e.source_id, e.target_id = new_src, new_tgt
            db.flush()

        repointed_mentions: list[str] = []
        deleted_mentions: list[dict] = []
        loser_mentions = db.scalars(
            select(NodeMention).where(NodeMention.node_id == loser.id)
        ).all()
        for m in loser_mentions:
            twin = db.scalar(
                select(NodeMention).where(
                    NodeMention.node_id == winner.id, NodeMention.document_id == m.document_id
                )
            )
            if twin is not None:
                deleted_mentions.append(_mention_row(m))
                db.delete(m)
            else:
                repointed_mentions.append(str(m.id))
                m.node_id = winner.id
            db.flush()

        winner_before = {
            "summary": winner.summary,
            "properties": winner.properties,
            "confidence": winner.confidence,
        }
        winner.properties = {**loser.properties, **winner.properties}  # winner keys win
        if not winner.summary and loser.summary:
            winner.summary = loser.summary
        embedding_backfilled = False
        if winner.embedding is None and loser.embedding is not None:
            winner.embedding = loser.embedding
            embedding_backfilled = True
        winner.confidence = max(winner.confidence, loser.confidence)
        loser.canonical_node_id = winner.id
        db.flush()

        return {
            "loser_id": str(loser.id),
            "winner_id": str(winner.id),
            "winner_before": winner_before,
            "embedding_backfilled": embedding_backfilled,
            "repointed_edges": repointed_edges,
            "folded_edges": folded_edges,
            "deleted_edges": deleted_edges,
            "repointed_mentions": repointed_mentions,
            "deleted_mentions": deleted_mentions,
        }

    # -- rollback ------------------------------------------------------------------
    def rollback(self, db: Session, rollback_data: dict) -> None:
        loser = db.get(Node, uuid.UUID(rollback_data["loser_id"]))
        winner = db.get(Node, uuid.UUID(rollback_data["winner_id"]))
        if loser is None or winner is None:
            raise ProposalError("Cannot roll back: a merged node no longer exists.")
        if loser.canonical_node_id != winner.id:
            raise ProposalError("Cannot roll back: the merge pointer was changed since apply.")

        loser.canonical_node_id = None
        before = rollback_data["winner_before"]
        winner.summary = before["summary"]
        winner.properties = before["properties"]
        winner.confidence = before["confidence"]
        if rollback_data["embedding_backfilled"]:
            winner.embedding = None
        db.flush()

        # Repointed rows go back to the loser first; only then can the deleted
        # rows re-insert without tripping the identity constraints.
        for r in rollback_data["repointed_edges"]:
            edge = db.get(Edge, uuid.UUID(r["id"]))
            if edge is not None:
                edge.source_id = uuid.UUID(r["source_id"])
                edge.target_id = uuid.UUID(r["target_id"])
        for m_id in rollback_data["repointed_mentions"]:
            mention = db.get(NodeMention, uuid.UUID(m_id))
            if mention is not None:
                mention.node_id = loser.id
        for f in rollback_data["folded_edges"]:
            edge = db.get(Edge, uuid.UUID(f["id"]))
            if edge is not None:
                edge.weight = f["weight"]
                edge.confidence = f["confidence"]
                edge.properties = f["properties"]
        db.flush()

        for row in rollback_data["deleted_edges"]:
            db.add(
                Edge(
                    id=uuid.UUID(row["id"]),
                    source_id=uuid.UUID(row["source_id"]),
                    target_id=uuid.UUID(row["target_id"]),
                    relationship_type=row["relationship_type"],
                    properties=row["properties"],
                    weight=row["weight"],
                    confidence=row["confidence"],
                    evidence_document_id=(
                        uuid.UUID(row["evidence_document_id"])
                        if row["evidence_document_id"]
                        else None
                    ),
                )
            )
        for row in rollback_data["deleted_mentions"]:
            db.add(
                NodeMention(
                    id=uuid.UUID(row["id"]),
                    node_id=uuid.UUID(row["node_id"]),
                    document_id=uuid.UUID(row["document_id"]),
                    context=row["context"],
                )
            )
        db.flush()

    # -- helpers -----------------------------------------------------------------
    @staticmethod
    def _nodes(db: Session, payload: dict) -> tuple[Node, Node]:
        try:
            loser_id = uuid.UUID(str(payload["loser_id"]))
            winner_id = uuid.UUID(str(payload["winner_id"]))
        except (KeyError, ValueError) as exc:
            raise ProposalError(f"Malformed entity_merge payload: {exc}") from exc
        loser = db.get(Node, loser_id)
        winner = db.get(Node, winner_id)
        if loser is None or winner is None:
            missing = loser_id if loser is None else winner_id
            raise ProposalError(f"Node {missing} does not exist.")
        return loser, winner


def merge_dedup_key(node_a: uuid.UUID, node_b: uuid.UUID) -> str:
    """Order-independent identity of a merge pair, for Proposal.dedup_key."""
    return ":".join(sorted((str(node_a), str(node_b))))
