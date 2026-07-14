"""schema_node_type / schema_relationship_type — evolve the ontology itself.

Where entity merges groom the graph's *content*, these proposals groom its
*vocabulary*. They are filed from two places:

  * **Drift** — during normal ingestion, the extractor reports entities that
    fit no ontology term (`ExtractionResult.unmapped_types`); `file_unmapped`
    turns them into proposals, one per proposed name, with the observing
    document as evidence.
  * **Bootstrap** — `python -m app.bootstrap` samples existing documents and
    asks the LLM which types the ontology is missing wholesale.

apply() is an INSERT into the registry — because the ontology is data, no
migration is needed, and the live-registry extractor picks the new term up on
the very next document. rollback() deletes the term, but only while nothing in
the graph uses it (FKs would block anyway; the check gives a clean error).
"""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Edge, Node, OntologyNodeType, OntologyRelationshipType
from app.db.models.proposal import Proposal
from app.db.models.source import RawDocument
from app.proposals.engine import ProposalEngine, ProposalError, register_handler

_NODE_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_REL_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

# Reviewer-facing prior for machine-proposed vocabulary: schema changes are
# cheap to apply but expensive to pollute, so they never auto-apply by default.
DRIFT_CONFIDENCE = 0.5


@register_handler
class SchemaNodeTypeHandler:
    kind = "schema_node_type"

    def validate(self, db: Session, payload: dict) -> None:
        name = payload.get("name", "")
        if not _NODE_NAME.match(name):
            raise ProposalError(f"'{name}' is not a valid snake_case node type name.")
        if db.get(OntologyNodeType, name) is not None:
            raise ProposalError(f"Node type '{name}' already exists in the registry.")

    def apply(self, db: Session, payload: dict) -> dict:
        db.add(OntologyNodeType(name=payload["name"], description=payload.get("description")))
        db.flush()
        return {"name": payload["name"]}

    def rollback(self, db: Session, rollback_data: dict) -> None:
        name = rollback_data["name"]
        in_use = db.scalar(select(func.count()).select_from(Node).where(Node.node_type == name))
        if in_use:
            raise ProposalError(f"Cannot roll back: {in_use} node(s) already use type '{name}'.")
        row = db.get(OntologyNodeType, name)
        if row is not None:
            db.delete(row)
            db.flush()


@register_handler
class SchemaRelationshipTypeHandler:
    kind = "schema_relationship_type"

    def validate(self, db: Session, payload: dict) -> None:
        name = payload.get("name", "")
        if not _REL_NAME.match(name):
            raise ProposalError(f"'{name}' is not a valid UPPER_SNAKE relationship name.")
        if db.get(OntologyRelationshipType, name) is not None:
            raise ProposalError(f"Relationship type '{name}' already exists in the registry.")

    def apply(self, db: Session, payload: dict) -> dict:
        db.add(
            OntologyRelationshipType(name=payload["name"], description=payload.get("description"))
        )
        db.flush()
        return {"name": payload["name"]}

    def rollback(self, db: Session, rollback_data: dict) -> None:
        name = rollback_data["name"]
        in_use = db.scalar(
            select(func.count()).select_from(Edge).where(Edge.relationship_type == name)
        )
        if in_use:
            raise ProposalError(
                f"Cannot roll back: {in_use} edge(s) already use relationship '{name}'."
            )
        row = db.get(OntologyRelationshipType, name)
        if row is not None:
            db.delete(row)
            db.flush()


def file_unmapped(
    db: Session, document: RawDocument | None, unmapped: list, *, agent: str = "drift"
) -> list[Proposal]:
    """Turn extractor drift reports (UnmappedType) into schema proposals.
    One proposal per proposed name across the whole graph's lifetime — the
    dedup key makes repeat observations (and rejections) idempotent."""
    engine = ProposalEngine(db)
    filed: list[Proposal] = []
    for item in unmapped:
        kind = "schema_node_type" if item.kind == "node" else "schema_relationship_type"
        dedup_key = item.name
        already = db.scalar(
            select(func.count())
            .select_from(Proposal)
            .where(Proposal.kind == kind, Proposal.dedup_key == dedup_key)
        )
        proposal = engine.submit(
            kind,
            {"name": item.name, "description": item.description},
            confidence=DRIFT_CONFIDENCE,
            agent=agent,
            evidence={
                "example": item.example,
                "document_id": str(document.id) if document is not None else None,
                "document_title": document.title if document is not None else None,
            },
            dedup_key=dedup_key,
        )
        if not already:
            filed.append(proposal)
    return filed
