"""Live ontology registry reader.

The seed lists in `ontology_seed.py` are only the *starting* vocabulary. Once
schema proposals extend the registry tables at runtime, the extractor must see
the database's current terms — both in the system prompt and in the structured-
output enums. This module is that bridge: read the registry, fall back to the
seed when the tables are empty (fresh DB before seeding)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.ontology import OntologyNodeType, OntologyRelationshipType
from app.db.ontology_seed import NODE_TYPES, RELATIONSHIP_TYPES


def live_registry(db: Session) -> tuple[list[dict], list[dict]]:
    """Return (node_types, relationship_types) as seed-shaped dicts."""
    node_rows = db.scalars(select(OntologyNodeType).order_by(OntologyNodeType.name)).all()
    rel_rows = db.scalars(
        select(OntologyRelationshipType).order_by(OntologyRelationshipType.name)
    ).all()
    if not node_rows or not rel_rows:
        return NODE_TYPES, RELATIONSHIP_TYPES
    return (
        [{"name": r.name, "description": r.description or ""} for r in node_rows],
        [
            {
                "name": r.name,
                "description": r.description or "",
                "allowed_source_types": r.allowed_source_types,
                "allowed_target_types": r.allowed_target_types,
            }
            for r in rel_rows
        ],
    )
