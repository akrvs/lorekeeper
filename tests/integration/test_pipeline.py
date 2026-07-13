"""Full pipeline: real connectors (MockTransport) → dedup → provenance → traversal."""

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.db.models import Edge, Node, NodeMention, RawDocument
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
