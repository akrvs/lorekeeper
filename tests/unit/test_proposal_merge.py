"""Proposal engine + entity_merge: lifecycle, graph surgery, and exact rollback."""

import pytest
from sqlalchemy import or_, select

from app.config import settings
from app.db.models import Edge, Node, NodeMention, Proposal, RawDocument
from app.llm.stub import deterministic_embedding
from app.proposals import ProposalEngine, ProposalError, merge_dedup_key
from app.repositories.graph import GraphRepository
from app.security.principal import Principal


def _node(db, name, *, node_type="feature", summary=None, embed=False, **props) -> Node:
    node = Node(
        node_type=node_type,
        name=name,
        summary=summary,
        properties=props,
        embedding=deterministic_embedding(name, settings.embedding_dim) if embed else None,
    )
    db.add(node)
    db.flush()
    return node


def _edge(db, src, tgt, rel, *, weight=1.0) -> Edge:
    edge = Edge(source_id=src.id, target_id=tgt.id, relationship_type=rel, weight=weight)
    db.add(edge)
    db.flush()
    return edge


def _doc(db, ext) -> RawDocument:
    doc = RawDocument(source_system="github", source_type="issue", external_id=ext)
    db.add(doc)
    db.flush()
    return doc


def _mention(db, node, doc) -> NodeMention:
    mention = NodeMention(node_id=node.id, document_id=doc.id)
    db.add(mention)
    db.flush()
    return mention


def _submit_merge(engine, loser, winner, *, confidence=0.8) -> Proposal:
    return engine.submit(
        "entity_merge",
        {"loser_id": str(loser.id), "winner_id": str(winner.id)},
        confidence=confidence,
        agent="test",
        dedup_key=merge_dedup_key(loser.id, winner.id),
    )


@pytest.fixture
def merge_graph(db):
    """Two duplicate features plus the neighborhood that exercises every merge case:
    a repointed edge, a folded edge (both nodes assert the same fact), an edge
    between the duplicates (self-loop after merge), and shared/unique mentions."""
    winner = _node(db, "checkout-v2", summary=None, embed=True, owner="alice")
    loser = _node(db, "checkout v2", summary="One-click checkout.", embed=True, status="live")
    other = _node(db, "payments-service")
    shared = _node(db, "incident-42", node_type="incident")

    edges = {
        "repointed": _edge(db, loser, other, "REFERENCES"),
        "winner_twin": _edge(db, winner, shared, "RELATES_TO", weight=2.0),
        "folded": _edge(db, loser, shared, "RELATES_TO", weight=3.0),
        "self_loop": _edge(db, loser, winner, "REFERENCES"),
    }
    d1, d2 = _doc(db, "1"), _doc(db, "2")
    _mention(db, loser, d1)  # only the loser saw d1 -> repointed
    _mention(db, loser, d2)  # both saw d2 -> loser's copy deleted
    _mention(db, winner, d2)
    return {"winner": winner, "loser": loser, "other": other, "shared": shared, "edges": edges}


def test_submit_is_deduplicated(db, merge_graph):
    engine = ProposalEngine(db)
    first = _submit_merge(engine, merge_graph["loser"], merge_graph["winner"])
    assert first.status == "pending"
    again = _submit_merge(engine, merge_graph["loser"], merge_graph["winner"])
    assert again.id == first.id
    assert db.scalar(select(Proposal).where(Proposal.id == first.id)) is not None


def test_approve_merges_the_full_neighborhood(db, merge_graph):
    winner, loser = merge_graph["winner"], merge_graph["loser"]
    engine = ProposalEngine(db)
    proposal = _submit_merge(engine, loser, winner)
    engine.approve(proposal.id, reviewed_by="tester")

    assert proposal.status == "applied"
    assert proposal.reviewed_by == "tester"
    assert loser.canonical_node_id == winner.id

    # Loser has no edges left; the winner carries the whole neighborhood.
    assert (
        db.scalar(select(Edge).where(or_(Edge.source_id == loser.id, Edge.target_id == loser.id)))
        is None
    )
    repointed = db.get(Edge, merge_graph["edges"]["repointed"].id)
    assert repointed.source_id == winner.id  # loser->other now winner->other
    twin = db.get(Edge, merge_graph["edges"]["winner_twin"].id)
    assert twin.weight == 5.0  # folded edge's weight absorbed (2 + 3)
    assert db.get(Edge, merge_graph["edges"]["folded"].id) is None
    assert db.get(Edge, merge_graph["edges"]["self_loop"].id) is None  # dropped self-loop

    # Mentions: winner now cites both documents, exactly once each.
    mentions = db.scalars(select(NodeMention).where(NodeMention.node_id == winner.id)).all()
    assert len(mentions) == 2
    assert db.scalar(select(NodeMention).where(NodeMention.node_id == loser.id)) is None

    # Enrichment: loser's summary/properties folded in, winner keys winning.
    assert winner.summary == "One-click checkout."
    assert winner.properties == {"status": "live", "owner": "alice"}


def test_rollback_restores_exact_prior_state(db, merge_graph):
    winner, loser = merge_graph["winner"], merge_graph["loser"]
    engine = ProposalEngine(db)
    proposal = _submit_merge(engine, loser, winner)
    engine.approve(proposal.id, reviewed_by="tester")
    engine.rollback(proposal.id, reviewed_by="tester")

    assert proposal.status == "rolled_back"
    assert loser.canonical_node_id is None
    assert winner.summary is None
    assert winner.properties == {"owner": "alice"}

    loser_edges = db.scalars(select(Edge).where(Edge.source_id == loser.id)).all()
    assert {(e.target_id, e.relationship_type) for e in loser_edges} == {
        (merge_graph["other"].id, "REFERENCES"),
        (merge_graph["shared"].id, "RELATES_TO"),
        (winner.id, "REFERENCES"),
    }
    assert db.get(Edge, merge_graph["edges"]["winner_twin"].id).weight == 2.0
    assert len(db.scalars(select(NodeMention).where(NodeMention.node_id == loser.id)).all()) == 2


def test_reject_is_sticky_and_touches_nothing(db, merge_graph):
    winner, loser = merge_graph["winner"], merge_graph["loser"]
    engine = ProposalEngine(db)
    proposal = _submit_merge(engine, loser, winner)
    engine.reject(proposal.id, reviewed_by="tester")

    assert proposal.status == "rejected"
    assert loser.canonical_node_id is None
    # A rescan re-filing the same pair gets the rejected row back, not a new one.
    again = _submit_merge(engine, loser, winner)
    assert again.id == proposal.id and again.status == "rejected"
    with pytest.raises(ProposalError, match="rejected"):
        engine.approve(proposal.id, reviewed_by="tester")


def test_validate_rejects_bad_merges(db, merge_graph):
    engine = ProposalEngine(db)
    winner, loser, shared = merge_graph["winner"], merge_graph["loser"], merge_graph["shared"]

    for loser_arg, winner_arg, match in (
        (winner, winner, "into itself"),
        (loser, shared, "across node types"),
    ):
        proposal = engine.submit(
            "entity_merge",
            {"loser_id": str(loser_arg.id), "winner_id": str(winner_arg.id)},
            confidence=0.8,
            agent="test",
        )
        with pytest.raises(ProposalError):
            engine.approve(proposal.id, reviewed_by="tester")
        assert db.get(Proposal, proposal.id).status == "failed"
        assert match in db.get(Proposal, proposal.id).error


def test_auto_apply_threshold(db, merge_graph, monkeypatch):
    monkeypatch.setattr(settings, "proposal_auto_apply_threshold", 0.9)
    engine = ProposalEngine(db)
    proposal = _submit_merge(engine, merge_graph["loser"], merge_graph["winner"], confidence=0.95)
    assert proposal.status == "auto_applied"
    assert merge_graph["loser"].canonical_node_id == merge_graph["winner"].id
    # Below the threshold nothing applies — it queues.
    a, b = _node(db, "svc-a"), _node(db, "svc-b")
    queued = _submit_merge(engine, a, b, confidence=0.5)
    assert queued.status == "pending"


def test_failed_apply_records_error(db, merge_graph):
    winner, loser = merge_graph["winner"], merge_graph["loser"]
    engine = ProposalEngine(db)
    proposal = _submit_merge(engine, loser, winner)
    db.delete(loser)  # graph changed under the pending proposal
    db.flush()
    with pytest.raises(ProposalError, match="does not exist"):
        engine.approve(proposal.id, reviewed_by="tester")
    refetched = db.get(Proposal, proposal.id)
    assert refetched.status == "failed"
    assert "does not exist" in refetched.error


def test_merged_nodes_hidden_from_semantic_search(db, merge_graph):
    winner, loser = merge_graph["winner"], merge_graph["loser"]
    engine = ProposalEngine(db)
    proposal = _submit_merge(engine, loser, winner)
    engine.approve(proposal.id, reviewed_by="tester")

    repo = GraphRepository(db, Principal.anonymous())
    qvec = deterministic_embedding("checkout v2", settings.embedding_dim)
    hits = {row[0].id for row in repo.semantic_search(qvec, node_type="feature", limit=10)}
    assert winner.id in hits
    assert loser.id not in hits
