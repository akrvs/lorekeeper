"""Resolver edge cases: canonical-pointer cycles + conflicting property merges."""

from sqlalchemy import select

from app.db.models import Node
from app.ontology.resolver import Resolver, ResolveStats
from tests.fixtures import N


def _feature(db, name: str) -> Node:
    node = Node(node_type="feature", name=name, properties={})
    db.add(node)
    db.flush()
    return node


def test_canonical_cycle_terminates(db):
    a = _feature(db, "checkout-v2")
    b = _feature(db, "checkout-v2")
    a.canonical_node_id, b.canonical_node_id = b.id, a.id  # A <-> B cycle
    db.flush()
    # Must not loop forever; returns a stable id within the cycle.
    assert Resolver(db)._canonical_id(a.id) in {a.id, b.id}


def test_conflicting_property_merge_last_writer_wins(db, provider):
    resolver = Resolver(db)
    stats = ResolveStats()
    vec = provider.embed(["checkout-v2\none-click checkout"])[0]
    resolver._resolve_node(
        N("n1", "feature", "checkout-v2", "one-click checkout", status="open"), vec, stats
    )
    resolver._resolve_node(
        N("n1", "feature", "checkout-v2", "one-click checkout", status="resolved"), vec, stats
    )
    features = db.scalars(select(Node).where(Node.node_type == "feature")).all()
    assert len(features) == 1  # deduped by trigram/vector
    assert features[0].properties["status"] == "resolved"  # later observation wins
    assert stats.nodes_merged == 1
