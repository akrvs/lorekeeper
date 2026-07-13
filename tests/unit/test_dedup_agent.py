"""Dedup agent: candidate discovery, winner selection, idempotent re-scans."""

from app.agents import AgentFactory
from app.config import settings
from app.db.models import Node, NodeMention, RawDocument
from app.llm.stub import deterministic_embedding
from app.proposals import ProposalEngine


def _user(db, name, *, source=None, ext=None, embed_text=None) -> Node:
    node = Node(
        node_type="user",
        name=name,
        properties={},
        source_system=source,
        external_id=ext,
        embedding=(
            deterministic_embedding(embed_text, settings.embedding_dim) if embed_text else None
        ),
    )
    db.add(node)
    db.flush()
    return node


def _mention(db, node) -> None:
    doc = RawDocument(
        source_system=node.source_system or "github",
        source_type="profile",
        external_id=f"doc-{node.id}",
    )
    db.add(doc)
    db.flush()
    db.add(NodeMention(node_id=node.id, document_id=doc.id))
    db.flush()


def test_cross_source_duplicates_are_proposed(db):
    """GitHub alice and Slack alice: the resolver never compares sourced nodes,
    so this exact pair is the agent's reason to exist."""
    gh = _user(db, "alice", source="github", ext="alice")
    slack = _user(db, "alice", source="slack", ext="U_ALICE")
    _mention(db, gh)  # more provenance -> preferred winner

    filed = AgentFactory.create(db, "dedup").scan()
    assert len(filed) == 1
    p = filed[0]
    assert p.kind == "entity_merge" and p.status == "pending" and p.agent == "dedup"
    assert p.payload["winner_id"] == str(gh.id)  # both sourced -> mentions decide
    assert p.payload["loser_id"] == str(slack.id)
    assert p.evidence["name_similarity"] == 1.0


def test_same_source_pairs_are_skipped(db):
    """Two distinct GitHub ids are two distinct accounts, however similar."""
    _user(db, "alice", source="github", ext="alice")
    _user(db, "alice", source="github", ext="alice2")
    assert AgentFactory.create(db, "dedup").scan() == []


def test_sourced_beats_derived_and_semantic_signal_counts(db):
    """A derived twin with an identical embedding merges INTO the sourced node,
    even when at least one signal (here: the vector) is what caught it."""
    sourced = _user(db, "robert tables", source="github", ext="bobby", embed_text="bobby tables")
    derived = _user(db, "bobby", embed_text="bobby tables")  # same text -> distance 0.0

    filed = AgentFactory.create(db, "dedup").scan()
    assert len(filed) == 1
    p = filed[0]
    assert p.payload["winner_id"] == str(sourced.id)
    assert p.payload["loser_id"] == str(derived.id)
    assert p.evidence["cosine_distance"] < 0.01
    assert p.confidence > 0.5


def test_rescan_is_idempotent_and_respects_decisions(db):
    _user(db, "carol", source="github", ext="carol")
    _user(db, "carol", source="slack", ext="U_CAROL")
    agent = AgentFactory.create(db, "dedup")

    filed = agent.scan()
    assert len(filed) == 1
    assert agent.scan() == []  # already filed -> nothing new

    ProposalEngine(db).reject(filed[0].id, reviewed_by="tester")
    assert agent.scan() == []  # rejection is sticky

    # Once a pair is actually merged, neither node is a candidate again.
    c = _user(db, "dave", source="github", ext="dave")
    d = _user(db, "dave", source="slack", ext="U_DAVE")
    (p,) = agent.scan()
    ProposalEngine(db).approve(p.id, reviewed_by="tester")
    assert d.canonical_node_id == c.id or c.canonical_node_id == d.id
    assert agent.scan() == []
