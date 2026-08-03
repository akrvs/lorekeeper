"""Importance job: PageRank on a known hub-and-spoke graph."""

from app.agents.importance import compute_importance, pagerank
from app.db.models import Edge, Node


def test_pagerank_hub_wins():
    edges = [("a", "hub"), ("b", "hub"), ("hub", "a")]
    scores = pagerank(["a", "b", "hub"], edges)
    assert scores["hub"] == 1.0
    assert scores["a"] > scores["b"]  # a receives the hub's outbound mass
    assert scores["b"] < 1.0


def test_pagerank_empty_graph():
    assert pagerank([], []) == {}


def _node(name):
    return Node(
        node_type="service", name=name, properties={}, source_system="github", external_id=name
    )


def test_compute_importance_stamps_properties(db):
    hub, spoke_a, spoke_b = _node("hub"), _node("spoke-a"), _node("spoke-b")
    db.add_all([hub, spoke_a, spoke_b])
    db.flush()
    db.add_all(
        [
            Edge(source_id=spoke_a.id, target_id=hub.id, relationship_type="REFERENCES"),
            Edge(source_id=spoke_b.id, target_id=hub.id, relationship_type="REFERENCES"),
        ]
    )
    db.flush()

    assert compute_importance(db) == 3
    db.refresh(hub)
    db.refresh(spoke_a)
    assert hub.properties["importance"] == 1.0
    assert 0.0 < spoke_a.properties["importance"] < 1.0
    # Idempotent: a second run leaves the same scores in place.
    assert compute_importance(db) == 3
    db.refresh(hub)
    assert hub.properties["importance"] == 1.0
