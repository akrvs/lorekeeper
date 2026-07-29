"""Full pipeline: real connectors (MockTransport) → dedup → provenance → traversal."""

from sqlalchemy import func, select, text
from sqlalchemy.orm import aliased

from app.config import settings
from app.db.models import Edge, Node, NodeMention, RawDocument
from app.llm.stub import StubProvider
from app.pipeline import run_source
from tests.fixtures import populate


def test_ingest_dedup_and_provenance(db, provider):
    populate(db, provider)

    assert db.scalar(select(func.count()).select_from(RawDocument)) == 3
    # repository appears in 2 GitHub docs -> one node (sourced upsert)
    assert len(db.scalars(select(Node).where(Node.node_type == "repository")).all()) == 1
    # checkout-v2 feature surfaced in GitHub + Slack -> one node (derived dedup)
    features = db.scalars(select(Node).where(Node.node_type == "feature")).all()
    assert len(features) == 1
    assert len(db.scalars(select(Node).where(Node.node_type == "incident")).all()) == 1
    # provenance: the one feature node is mentioned in both source documents
    mentions = db.scalar(
        select(func.count()).select_from(NodeMention).where(NodeMention.node_id == features[0].id)
    )
    assert mentions == 2
    assert features[0].embedding is not None


class _CountingStub(StubProvider):
    def __init__(self):
        super().__init__()
        self.extract_calls = 0

    def extract(self, system_prompt, user_content, schema):
        self.extract_calls += 1
        return super().extract(system_prompt, user_content, schema)


def test_unchanged_documents_skip_extraction(db, tmp_path, monkeypatch):
    db.execute(
        text(
            "TRUNCATE node_mentions, edges, nodes, raw_documents, ingestion_runs RESTART IDENTITY CASCADE"
        )
    )
    db.commit()
    (tmp_path / "a.md").write_text("alpha references [[b]]")
    (tmp_path / "b.md").write_text("beta")
    monkeypatch.setattr(settings, "local_root", str(tmp_path))
    counting = _CountingStub()

    first = run_source(db, "local", provider=counting)
    assert first["documents"] == 2 and first["documents_unchanged"] == 0
    assert counting.extract_calls == 2

    second = run_source(db, "local", provider=counting)
    assert second["documents_unchanged"] == 2
    assert counting.extract_calls == 2

    (tmp_path / "a.md").write_text("alpha CHANGED, still references [[b]]")
    third = run_source(db, "local", provider=counting)
    assert third["documents_unchanged"] == 1
    assert counting.extract_calls == 3

    forced = run_source(db, "local", provider=counting, force=True)
    assert forced["documents_unchanged"] == 0
    assert counting.extract_calls == 5


def test_headline_traversal(db, provider):
    populate(db, provider)
    Repo, Incident, Feature, Thread = (aliased(Node) for _ in range(4))
    e_aff, e_cause, e_disc = (aliased(Edge) for _ in range(3))
    q = (
        select(Thread.name)
        .select_from(Repo)
        .join(e_aff, (e_aff.relationship_type == "AFFECTS") & (e_aff.target_id == Repo.id))
        .join(Incident, (Incident.id == e_aff.source_id) & (Incident.node_type == "incident"))
        .join(e_cause, (e_cause.relationship_type == "CAUSED") & (e_cause.target_id == Incident.id))
        .join(Feature, (Feature.id == e_cause.source_id) & (Feature.node_type == "feature"))
        .join(e_disc, (e_disc.relationship_type == "DISCUSSES") & (e_disc.target_id == Feature.id))
        .join(Thread, (Thread.id == e_disc.source_id) & (Thread.node_type == "slack_thread"))
        .where(Repo.external_id == "acme/checkout-service")
        .distinct()
    )
    assert len(db.execute(q).all()) >= 1
