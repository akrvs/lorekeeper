"""Read-only MCP graph tools driven exactly as an MCP client would drive them
(each tool opens its own session against committed data)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.config import settings
from app.db.models import Edge, Node
from app.db.models.proposal import Proposal
from app.db.session import SessionLocal
from app.llm.stub import deterministic_embedding
from app.mcp.server import find_connection, get_recent_changes, get_stale_nodes

_CLEANUP = "node_mentions, edges, nodes, raw_documents, ingestion_runs, proposals, audit_log"


def _node(name, node_type="service", properties=None, source="github"):
    return Node(
        node_type=node_type,
        name=name,
        properties=properties or {},
        source_system=source,
        external_id=name,
        embedding=deterministic_embedding(name, settings.embedding_dim),
    )


@pytest.fixture
def committed_nodes(schema):
    with SessionLocal() as db:
        db.execute(text(f"TRUNCATE {_CLEANUP} RESTART IDENTITY CASCADE"))
        db.commit()
        old = _node("legacy-runbook", properties={"stale": True, "stale_since": "2024-01-15"})
        new = _node("old-dashboard", properties={"stale": True, "stale_since": "2025-06-01"})
        fresh = _node("payments-service")
        db.add_all([old, new, fresh])
        db.commit()
        ids = {"old": str(old.id), "new": str(new.id), "fresh": str(fresh.id)}
    yield ids
    with SessionLocal() as s:
        s.execute(text(f"TRUNCATE {_CLEANUP} RESTART IDENTITY CASCADE"))
        s.commit()


def test_get_stale_nodes_lists_oldest_first(committed_nodes):
    ids = committed_nodes
    out = get_stale_nodes()
    assert "2 stale-flagged node(s)" in out
    assert ids["old"] in out and ids["new"] in out
    assert ids["fresh"] not in out
    assert out.index("legacy-runbook") < out.index("old-dashboard")
    assert "stale_since=2024-01-15" in out


def test_get_stale_nodes_empty_queue(schema):
    with SessionLocal() as db:
        db.execute(text(f"TRUNCATE {_CLEANUP} RESTART IDENTITY CASCADE"))
        db.commit()
    assert "No stale-flagged nodes" in get_stale_nodes()


@pytest.fixture
def committed_changes(schema):
    now = datetime.now(UTC)
    with SessionLocal() as db:
        db.execute(text(f"TRUNCATE {_CLEANUP} RESTART IDENTITY CASCADE"))
        db.commit()
        fresh = _node("brand-new-service")
        old = _node("ancient-service")
        old.created_at = now - timedelta(days=30)
        db.add_all([fresh, old])
        db.add(
            Proposal(
                kind="stale_flag",
                status="applied",
                payload={"node_id": "x", "reason": "evidence quiet for 200 days"},
                confidence=0.8,
                agent="staleness",
                applied_at=now,
            )
        )
        db.add(
            Proposal(
                kind="stale_flag",
                status="pending",
                payload={"node_id": "y", "reason": "still waiting for review"},
                confidence=0.5,
                agent="staleness",
            )
        )
        db.commit()
        ids = {"fresh": str(fresh.id), "old": str(old.id)}
    yield ids
    with SessionLocal() as s:
        s.execute(text(f"TRUNCATE {_CLEANUP} RESTART IDENTITY CASCADE"))
        s.commit()


@pytest.fixture
def committed_chain(schema):
    """alice -[AUTHORED]-> pr-42 -[IMPLEMENTS]-> checkout, plus an island."""
    with SessionLocal() as db:
        db.execute(text(f"TRUNCATE {_CLEANUP} RESTART IDENTITY CASCADE"))
        db.commit()
        alice = _node("alice", node_type="user")
        pr = _node("pr-42", node_type="pull_request")
        feature = _node("checkout", node_type="feature")
        island = _node("island", node_type="service")
        db.add_all([alice, pr, feature, island])
        db.flush()
        db.add_all(
            [
                Edge(source_id=alice.id, target_id=pr.id, relationship_type="AUTHORED"),
                Edge(source_id=pr.id, target_id=feature.id, relationship_type="IMPLEMENTS"),
            ]
        )
        db.commit()
        ids = {k: str(n.id) for k, n in [("a", alice), ("f", feature), ("i", island)]}
    yield ids
    with SessionLocal() as s:
        s.execute(text(f"TRUNCATE {_CLEANUP} RESTART IDENTITY CASCADE"))
        s.commit()


def test_find_connection_shortest_path(committed_chain):
    ids = committed_chain
    out = find_connection(ids["a"], ids["f"])
    assert "CONNECTION (2 hop(s))" in out
    assert "AUTHORED" in out and "IMPLEMENTS" in out
    assert "alice" in out and "checkout" in out


def test_find_connection_no_path_and_errors(committed_chain):
    ids = committed_chain
    assert "No path between" in find_connection(ids["a"], ids["i"])
    assert "not a valid UUID" in find_connection("nope", ids["a"])
    assert "same node" in find_connection(ids["a"], ids["a"])


def test_get_recent_changes_windows_and_sections(committed_changes):
    ids = committed_changes
    out = get_recent_changes(days=7)
    assert "NEW ENTITIES (1)" in out
    assert ids["fresh"] in out and ids["old"] not in out
    assert "APPLIED MAINTENANCE (1)" in out
    assert "evidence quiet for 200 days" in out
    assert "still waiting for review" not in out
    # A wider window picks up the old node too.
    assert ids["old"] in get_recent_changes(days=60)
