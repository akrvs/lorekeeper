"""Staleness agent: quiet nodes flagged, fresh nodes spared, exact rollback."""

from datetime import UTC, datetime, timedelta

from app.agents import AgentFactory
from app.db.models import Node, NodeMention, RawDocument
from app.proposals import ProposalEngine


def _node_with_mention(db, name, *, age_days: int, node_type="feature", props=None) -> Node:
    node = Node(node_type=node_type, name=name, properties=props or {})
    doc = RawDocument(
        source_system="github",
        source_type="issue",
        external_id=f"doc-{name}",
        source_updated_at=datetime.now(UTC) - timedelta(days=age_days),
    )
    db.add_all([node, doc])
    db.flush()
    db.add(NodeMention(node_id=node.id, document_id=doc.id))
    db.flush()
    return node


def test_quiet_nodes_are_flagged_and_fresh_ones_spared(db):
    old = _node_with_mention(db, "legacy-cart", age_days=400)
    fresh = _node_with_mention(db, "checkout-v2", age_days=3)

    filed = AgentFactory.create(db, "staleness").scan()
    assert [p.payload["node_id"] for p in filed] == [str(old.id)]
    p = filed[0]
    assert p.kind == "stale_flag" and p.agent == "staleness"
    assert p.confidence > 0.5  # well past the threshold -> higher confidence
    assert str(fresh.id) not in {q.payload["node_id"] for q in filed}


def test_apply_sets_flag_and_rollback_restores(db):
    node = _node_with_mention(db, "legacy-cart", age_days=400, props={"owner": "alice"})
    engine = ProposalEngine(db)
    (p,) = AgentFactory.create(db, "staleness").scan()

    engine.approve(p.id, reviewed_by="tester")
    assert node.properties["stale"] is True
    assert node.properties["stale_since"] == p.payload["last_seen"]
    assert node.properties["owner"] == "alice"  # untouched

    # A flagged node is not re-proposed...
    assert AgentFactory.create(db, "staleness").scan() == []

    engine.rollback(p.id, reviewed_by="tester")
    assert node.properties == {"owner": "alice"}


def test_rescan_is_idempotent(db):
    _node_with_mention(db, "legacy-cart", age_days=400)
    agent = AgentFactory.create(db, "staleness")
    assert len(agent.scan()) == 1
    assert agent.scan() == []  # same node, same last_seen -> dedup key blocks refile
