"""Contradiction agent: conflicting cross-source facts become disputed stamps."""

from app.agents import AgentFactory
from app.db.models import Node
from app.proposals import ProposalEngine


def _pair(db):
    canonical = Node(
        node_type="service",
        name="payments",
        properties={"owner": "alice", "tier": 1},
        source_system="github",
        external_id="p1",
    )
    db.add(canonical)
    db.flush()
    duplicate = Node(
        node_type="service",
        name="payments",
        properties={"owner": "bob", "tier": 1, "stale": True},
        source_system="slack",
        external_id="p2",
        canonical_node_id=canonical.id,
    )
    db.add(duplicate)
    db.flush()
    return canonical, duplicate


def test_scan_files_only_real_conflicts(db):
    canonical, _ = _pair(db)
    filed = AgentFactory.create(db, "contradiction").scan()
    assert len(filed) == 1  # tier matches, stale is a system key — only owner
    proposal = filed[0]
    assert proposal.kind == "fact_conflict"
    assert proposal.payload["property"] == "owner"
    values = {v["source"]: v["value"] for v in proposal.payload["values"]}
    assert values == {"github": "alice", "slack": "bob"}
    assert proposal.evidence["canonical"]["id"] == str(canonical.id)

    # Sticky: a rescan re-files nothing.
    assert AgentFactory.create(db, "contradiction").scan() == []


def test_apply_stamps_disputed_and_rollback_restores(db):
    canonical, _ = _pair(db)
    (proposal,) = AgentFactory.create(db, "contradiction").scan()
    engine = ProposalEngine(db)

    engine.approve(proposal.id, reviewed_by="tester")
    db.refresh(canonical)
    assert canonical.properties["owner"] == "alice"  # no winner is picked
    assert {v["value"] for v in canonical.properties["disputed"]["owner"]} == {"alice", "bob"}
    # A disputed key is not re-filed even under a fresh dedup horizon.
    assert AgentFactory.create(db, "contradiction").scan() == []

    engine.rollback(proposal.id, reviewed_by="tester")
    db.refresh(canonical)
    assert "disputed" not in canonical.properties
