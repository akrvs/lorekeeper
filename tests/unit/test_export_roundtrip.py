"""Export/import round-trip: dump a small graph, wipe it, restore it intact."""

from app.config import settings
from app.db.models import Edge, Node, NodeMention, RawDocument
from app.export import cmd_dump, cmd_load
from app.llm.stub import deterministic_embedding


def _seed(db):
    doc = RawDocument(
        source_system="github",
        source_type="pull_request",
        external_id="pr-1",
        title="Add checkout",
        content="checkout work",
    )
    alice = Node(
        node_type="user",
        name="alice",
        properties={"role": "sre"},
        source_system="github",
        external_id="alice",
        embedding=deterministic_embedding("alice", settings.embedding_dim),
    )
    pr = Node(
        node_type="pull_request",
        name="pr-1",
        properties={},
        source_system="github",
        external_id="pr-1",
    )
    db.add_all([doc, alice, pr])
    db.flush()
    alias = Node(
        node_type="user",
        name="alice-slack",
        properties={},
        source_system="slack",
        external_id="U_ALICE",
        canonical_node_id=alice.id,
    )
    db.add(alias)
    db.add(
        Edge(
            source_id=alice.id,
            target_id=pr.id,
            relationship_type="AUTHORED",
            evidence_document_id=doc.id,
        )
    )
    db.add(NodeMention(node_id=alice.id, document_id=doc.id, context="alice opened pr-1"))
    db.flush()


def test_dump_load_roundtrip(db, tmp_path):
    _seed(db)
    path = str(tmp_path / "graph.jsonl")
    assert cmd_dump(db, path) == 0

    db.query(NodeMention).delete()
    db.query(Edge).delete()
    db.query(Node).delete()
    db.query(RawDocument).delete()
    db.flush()

    assert cmd_load(db, path) == 0
    alice = db.query(Node).filter_by(name="alice").one()
    assert alice.properties == {"role": "sre"}
    assert alice.embedding is not None and len(alice.embedding) == settings.embedding_dim
    alias = db.query(Node).filter_by(name="alice-slack").one()
    assert alias.canonical_node_id == alice.id
    edge = db.query(Edge).one()
    assert edge.relationship_type == "AUTHORED"
    mention = db.query(NodeMention).one()
    assert mention.context == "alice opened pr-1"
    assert db.query(RawDocument).one().title == "Add checkout"


def test_load_refuses_non_empty(db, tmp_path):
    _seed(db)
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert cmd_load(db, str(path)) == 1
