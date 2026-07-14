"""Schema drift → proposal → live registry: the ontology actually evolves."""

import pytest
from sqlalchemy import select

from app.db.models import Node, OntologyNodeType, OntologyRelationshipType
from app.ontology.extractor import build_system_prompt
from app.ontology.registry import live_registry
from app.ontology.schema import UnmappedType, extraction_model_for
from app.proposals import ProposalEngine, ProposalError, file_unmapped


def _unmapped(kind="node", name="design_doc"):
    return UnmappedType(
        kind=kind,
        name=name,
        description="An architecture/design document.",
        example="the checkout-v2 design doc",
    )


def test_drift_files_one_proposal_per_name(db):
    filed = file_unmapped(db, None, [_unmapped(), _unmapped()])
    assert len(filed) == 1  # same name in one batch -> one proposal
    p = filed[0]
    assert p.kind == "schema_node_type" and p.agent == "drift" and p.status == "pending"
    # A later document reporting the same missing type files nothing new.
    assert file_unmapped(db, None, [_unmapped()]) == []


def test_approved_node_type_lands_in_registry_and_extraction_model(db):
    engine = ProposalEngine(db)
    (p,) = file_unmapped(db, None, [_unmapped(name="design_doc")])
    engine.approve(p.id, reviewed_by="tester")

    assert db.get(OntologyNodeType, "design_doc") is not None
    # The live-registry extraction path picks the new term up immediately:
    node_types, rel_types = live_registry(db)
    names = [nt["name"] for nt in node_types]
    assert "design_doc" in names
    model = extraction_model_for(names, [rt["name"] for rt in rel_types])
    parsed = model.model_validate(
        {
            "nodes": [
                {
                    "temp_id": "n1",
                    "node_type": "design_doc",
                    "name": "checkout-v2 design",
                    "summary": "The design doc.",
                }
            ]
        }
    )
    assert parsed.nodes[0].node_type.value == "design_doc"
    assert "design_doc" in build_system_prompt(node_types, rel_types)


def test_relationship_type_lifecycle_and_rollback_guard(db):
    engine = ProposalEngine(db)
    (p,) = file_unmapped(db, None, [_unmapped(kind="relationship", name="SUPERSEDES")])
    assert p.kind == "schema_relationship_type"
    engine.approve(p.id, reviewed_by="tester")
    assert db.get(OntologyRelationshipType, "SUPERSEDES") is not None

    engine.rollback(p.id, reviewed_by="tester")
    assert db.get(OntologyRelationshipType, "SUPERSEDES") is None


def test_node_type_rollback_blocked_while_in_use(db):
    engine = ProposalEngine(db)
    (p,) = file_unmapped(db, None, [_unmapped(name="runbook")])
    engine.approve(p.id, reviewed_by="tester")
    db.add(Node(node_type="runbook", name="oncall-runbook", properties={}))
    db.flush()

    with pytest.raises(ProposalError, match="already use"):
        engine.rollback(p.id, reviewed_by="tester")
    assert db.get(OntologyNodeType, "runbook") is not None  # registry untouched


def test_validation_rejects_bad_names_and_duplicates(db):
    engine = ProposalEngine(db)
    for kind, bad_name in (("node", "Design Doc!"), ("relationship", "supersedes")):
        (p,) = file_unmapped(db, None, [_unmapped(kind=kind, name=bad_name)])
        with pytest.raises(ProposalError):
            engine.approve(p.id, reviewed_by="tester")

    # Proposing an already-registered term fails validation cleanly.
    (dup,) = file_unmapped(db, None, [_unmapped(name="feature")])
    with pytest.raises(ProposalError, match="already exists"):
        engine.approve(dup.id, reviewed_by="tester")


def test_seeded_registry_matches_live_registry(db):
    node_types, rel_types = live_registry(db)
    names = {nt["name"] for nt in node_types}
    assert {"feature", "incident", "user"} <= names
    assert db.scalar(select(OntologyNodeType).where(OntologyNodeType.name == "feature"))
