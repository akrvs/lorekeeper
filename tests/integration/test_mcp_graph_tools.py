"""Read-only MCP graph tools driven exactly as an MCP client would drive them
(each tool opens its own session against committed data)."""

import pytest
from sqlalchemy import text

from app.config import settings
from app.db.models import Node
from app.db.session import SessionLocal
from app.llm.stub import deterministic_embedding
from app.mcp.server import get_stale_nodes

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
