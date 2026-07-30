import logging

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import aliased

from app.agents.base import AgentFactory, MaintenanceAgent
from app.config import settings
from app.db.models import Edge, Node, NodeMention
from app.db.models.proposal import Proposal

logger = logging.getLogger("company_brain.agents.hygiene")


@AgentFactory.register("hygiene")
class HygieneAgent(MaintenanceAgent):
    def scan(self) -> list[Proposal]:
        filed: list[Proposal] = []
        filed += self._file_orphans()
        filed += self._file_weak_edges()
        logger.info("hygiene scan complete: %d proposal(s) filed", len(filed))
        return filed

    def _file_orphans(self) -> list[Proposal]:
        alias_node = aliased(Node)
        stmt = (
            select(Node.id, Node.name, Node.node_type)
            .where(
                Node.canonical_node_id.is_(None),
                ~exists(
                    select(Edge.id).where(or_(Edge.source_id == Node.id, Edge.target_id == Node.id))
                ),
                ~exists(select(NodeMention.id).where(NodeMention.node_id == Node.id)),
                ~exists(select(alias_node.id).where(alias_node.canonical_node_id == Node.id)),
            )
            .limit(settings.hygiene_scan_limit)
        )
        filed = []
        for node_id, name, node_type in self.db.execute(stmt).all():
            dedup_key = f"orphan:{node_id}"
            if self._already_filed("node_removal", dedup_key):
                continue
            filed.append(
                self.engine.submit(
                    "node_removal",
                    {
                        "node_id": str(node_id),
                        "reason": f"orphan {node_type} '{name}': no edges, no mentions",
                    },
                    confidence=0.7,
                    agent="hygiene",
                    evidence={"name": name, "node_type": node_type},
                    dedup_key=dedup_key,
                )
            )
        return filed

    def _file_weak_edges(self) -> list[Proposal]:
        stmt = (
            select(Edge.id, Edge.relationship_type, Edge.confidence)
            .where(
                Edge.confidence < settings.hygiene_edge_confidence,
                Edge.evidence_document_id.is_(None),
            )
            .limit(settings.hygiene_scan_limit)
        )
        filed = []
        for edge_id, relationship, confidence in self.db.execute(stmt).all():
            dedup_key = f"weak-edge:{edge_id}"
            if self._already_filed("edge_removal", dedup_key):
                continue
            filed.append(
                self.engine.submit(
                    "edge_removal",
                    {
                        "edge_id": str(edge_id),
                        "reason": (
                            f"weak {relationship} edge: confidence {confidence:.2f}, no evidence"
                        ),
                    },
                    confidence=0.6,
                    agent="hygiene",
                    evidence={"relationship": relationship, "edge_confidence": confidence},
                    dedup_key=dedup_key,
                )
            )
        return filed

    def _already_filed(self, kind: str, dedup_key: str) -> bool:
        return (
            self.db.scalar(
                select(Proposal.id).where(Proposal.kind == kind, Proposal.dedup_key == dedup_key)
            )
            is not None
        )
