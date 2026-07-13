"""MCP proposal-review tools: the full agent → queue → decide → undo loop
driven exactly as an MCP client would drive it (tools open their own sessions)."""

import re

import pytest
from sqlalchemy import text

from app.agents import AgentFactory
from app.config import settings
from app.db.models import Node
from app.db.session import SessionLocal
from app.llm.stub import deterministic_embedding
from app.mcp.server import (
    approve_proposal,
    reject_proposal,
    review_proposals,
    rollback_proposal,
)

_CLEANUP = "node_mentions, edges, nodes, raw_documents, ingestion_runs, proposals, audit_log"


@pytest.fixture
def committed_duplicates(schema):
    """Commit a cross-source duplicate pair and file the merge proposal."""
    with SessionLocal() as db:
        db.execute(text(f"TRUNCATE {_CLEANUP} RESTART IDENTITY CASCADE"))
        db.commit()
        vec = deterministic_embedding("erin the SRE", settings.embedding_dim)
        gh = Node(
            node_type="user",
            name="erin",
            properties={},
            source_system="github",
            external_id="erin",
            embedding=vec,
        )
        slack = Node(
            node_type="user",
            name="erin",
            properties={},
            source_system="slack",
            external_id="U_ERIN",
            embedding=vec,
        )
        db.add_all([gh, slack])
        db.commit()
        (proposal,) = AgentFactory.create(db, "dedup").scan()
        ids = {"proposal": str(proposal.id), "gh": str(gh.id), "slack": str(slack.id)}
    yield ids
    with SessionLocal() as s:
        s.execute(text(f"TRUNCATE {_CLEANUP} RESTART IDENTITY CASCADE"))
        s.commit()


def test_review_approve_rollback_loop(committed_duplicates):
    ids = committed_duplicates

    queue = review_proposals()
    assert ids["proposal"] in queue and "merge 'erin' -> 'erin'" in queue

    res = approve_proposal(ids["proposal"])
    assert "'applied'" in res
    with SessionLocal() as db:
        loser_ids = {str(n.id) for n in db.query(Node).filter(Node.canonical_node_id.is_not(None))}
    assert len(loser_ids) == 1 and loser_ids <= {ids["gh"], ids["slack"]}
    assert "clean" in review_proposals()  # queue drained

    res = rollback_proposal(ids["proposal"])
    assert "'rolled_back'" in res
    with SessionLocal() as db:
        assert db.query(Node).filter(Node.canonical_node_id.is_not(None)).count() == 0


def test_reject_and_error_paths(committed_duplicates):
    ids = committed_duplicates
    assert ids["proposal"] in review_proposals()
    assert "'rejected'" in reject_proposal(ids["proposal"])
    # Deciding an already-decided proposal is a clean error, not a crash.
    err = approve_proposal(ids["proposal"])
    assert err.startswith("ERROR:") and "rejected" in err
    assert "not a valid UUID" in approve_proposal("nope")
    # The audit trail recorded every decision made through the MCP surface.
    with SessionLocal() as db:
        tools = {row[0] for row in db.execute(text("SELECT DISTINCT tool FROM audit_log")).all()}
    assert {"review_proposals", "reject_proposal"} <= tools


def test_review_proposals_filters_by_status(committed_duplicates):
    assert re.search(r"1 'pending' proposal", review_proposals(status="pending"))
    assert "clean" in review_proposals(status="applied")
