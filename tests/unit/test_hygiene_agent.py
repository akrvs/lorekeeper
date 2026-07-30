from sqlalchemy import select

from app.agents import AgentFactory
from app.db.models import Edge, Node, NodeMention, RawDocument
from app.proposals import ProposalEngine


def _mentioned_node(db, name: str) -> Node:
    node = Node(node_type="feature", name=name)
    doc = RawDocument(source_system="github", source_type="issue", external_id=f"doc-{name}")
    db.add_all([node, doc])
    db.flush()
    db.add(NodeMention(node_id=node.id, document_id=doc.id))
    db.flush()
    return node


def test_orphans_flagged_and_evidenced_nodes_spared(db):
    orphan = Node(node_type="feature", name="ghost-feature")
    db.add(orphan)
    db.flush()
    mentioned = _mentioned_node(db, "real-feature")

    filed = AgentFactory.create(db, "hygiene").scan()
    assert [p.payload["node_id"] for p in filed] == [str(orphan.id)]
    assert filed[0].kind == "node_removal"
    assert "ghost-feature" in filed[0].payload["reason"]
    assert str(mentioned.id) not in {p.payload.get("node_id") for p in filed}


def test_node_removal_applies_and_rolls_back(db):
    orphan = Node(node_type="feature", name="ghost", properties={"k": "v"})
    db.add(orphan)
    db.flush()
    orphan_id = orphan.id
    engine = ProposalEngine(db)
    (p,) = AgentFactory.create(db, "hygiene").scan()

    engine.approve(p.id, reviewed_by="tester")
    assert db.get(Node, orphan_id) is None

    engine.rollback(p.id, reviewed_by="tester")
    restored = db.get(Node, orphan_id)
    assert restored is not None
    assert restored.name == "ghost" and restored.properties == {"k": "v"}


def test_weak_edges_flagged_applied_and_rolled_back(db):
    a = _mentioned_node(db, "feat-a")
    b = _mentioned_node(db, "feat-b")
    edge = Edge(source_id=a.id, target_id=b.id, relationship_type="IMPLEMENTS", confidence=0.1)
    strong = Edge(source_id=b.id, target_id=a.id, relationship_type="DISCUSSES", confidence=0.9)
    db.add_all([edge, strong])
    db.flush()
    edge_id = edge.id

    filed = AgentFactory.create(db, "hygiene").scan()
    weak = [p for p in filed if p.kind == "edge_removal"]
    assert [p.payload["edge_id"] for p in weak] == [str(edge_id)]

    engine = ProposalEngine(db)
    engine.approve(weak[0].id, reviewed_by="tester")
    assert db.get(Edge, edge_id) is None
    assert db.get(Edge, strong.id) is not None

    engine.rollback(weak[0].id, reviewed_by="tester")
    restored = db.get(Edge, edge_id)
    assert restored is not None and restored.relationship_type == "IMPLEMENTS"


def test_rescan_is_idempotent(db):
    db.add(Node(node_type="feature", name="ghost"))
    db.flush()
    agent = AgentFactory.create(db, "hygiene")
    assert len(agent.scan()) == 1
    assert agent.scan() == []


def test_validation_blocks_connected_node(db):
    a = _mentioned_node(db, "feat-a")
    engine = ProposalEngine(db)
    proposal = engine.submit(
        "node_removal",
        {"node_id": str(a.id), "reason": "manual"},
        confidence=0.9,
        agent="tester",
        dedup_key=f"manual:{a.id}",
    )
    try:
        engine.approve(proposal.id, reviewed_by="tester")
        raise AssertionError("expected ProposalError")
    except Exception as exc:
        assert "mention" in str(exc)
    assert db.scalar(select(Node.id).where(Node.id == a.id)) is not None
