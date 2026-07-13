"""Phase 9: [[wikilinks]] resolve to real file nodes, not abstract concepts."""

from sqlalchemy import select

from app.connectors.local import LocalConnector, reconcile_wikilinks
from app.db.models import Edge, Node
from app.llm.stub import StubProvider
from app.pipeline import process_document


def _ingest(db, tmp_path):
    _run, docs = LocalConnector(db, root=str(tmp_path)).run()
    provider = StubProvider()
    for doc in docs:
        process_document(db, provider, doc)
    return reconcile_wikilinks(db)


def test_wikilink_resolves_to_file_node(db, tmp_path):
    (tmp_path / "alpha.md").write_text("# Alpha\nSee [[beta]] for details.")
    (tmp_path / "beta.md").write_text("# Beta\nStandalone note.")

    resolved = _ingest(db, tmp_path)
    assert resolved == 1

    # No detached concept nodes remain.
    concepts = db.scalars(
        select(Node).where(Node.source_system.is_(None), Node.node_type == "document")
    ).all()
    assert concepts == []

    # The [[beta]] reference points at the real beta.md file node.
    alpha = db.scalar(select(Node).where(Node.external_id == "alpha.md"))
    beta = db.scalar(select(Node).where(Node.external_id == "beta.md"))
    edge = db.scalar(
        select(Edge).where(
            Edge.source_id == alpha.id,
            Edge.target_id == beta.id,
            Edge.relationship_type == "REFERENCES",
        )
    )
    assert edge is not None


def test_unresolved_wikilink_stays_a_concept(db, tmp_path):
    (tmp_path / "alpha.md").write_text("# Alpha\nSee [[nonexistent-doc]].")

    resolved = _ingest(db, tmp_path)
    assert resolved == 0

    # A wikilink with no matching file remains a concept node (still useful).
    concept = db.scalar(
        select(Node).where(Node.source_system.is_(None), Node.name == "nonexistent-doc")
    )
    assert concept is not None
