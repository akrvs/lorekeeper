"""Digest job: summarizes a seeded window, stays quiet on an empty one."""

from datetime import UTC, datetime, timedelta

from app.agents.digest import build_digest
from app.db.models import Node
from app.db.models.proposal import Proposal
from app.db.models.source import IngestionRun, RawDocument


def _seed(db):
    now = datetime.now(UTC)
    db.add(
        IngestionRun(
            source_system="github",
            connector="GitHubConnector",
            status="completed",
            started_at=now,
            finished_at=now,
        )
    )
    db.add(
        RawDocument(
            source_system="github",
            source_type="pull_request",
            external_id="1",
            content="x",
            ingested_at=now,
        )
    )
    db.add(
        Node(
            node_type="service",
            name="svc",
            properties={},
            source_system="github",
            external_id="svc",
        )
    )
    db.add(
        Proposal(
            kind="entity_merge",
            status="applied",
            payload={},
            confidence=0.9,
            agent="dedup",
            applied_at=now,
        )
    )
    db.add(
        Proposal(
            kind="stale_flag",
            status="pending",
            payload={},
            confidence=0.5,
            agent="staleness",
        )
    )
    # Outside the window: must not be counted.
    db.add(
        IngestionRun(
            source_system="slack",
            connector="SlackConnector",
            status="completed",
            started_at=now - timedelta(days=40),
        )
    )
    db.flush()


def test_digest_summarizes_window(db):
    _seed(db)
    out = build_digest(db, days=7)
    assert "github: 1 run(s) completed" in out
    assert "slack" not in out
    assert "documents ingested: 1" in out
    assert "entities created: 1" in out
    assert "filed 1 entity_merge proposal(s)" in out
    assert "filed 1 stale_flag proposal(s)" in out
    assert "applied 1 entity_merge proposal(s)" in out
    assert "pending review now: 1" in out


def test_digest_empty_window(db):
    out = build_digest(db, days=1)
    assert "(no runs)" in out
    assert "pending review now:" in out
