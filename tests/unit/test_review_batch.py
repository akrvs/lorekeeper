"""approve-all: batch approval respects the confidence floor and survives
per-item failures without aborting the run."""

from app.db.models import Node
from app.db.models.proposal import Proposal
from app.review import cmd_approve_all


def _stale_proposal(node_id, confidence):
    return Proposal(
        kind="stale_flag",
        payload={"node_id": str(node_id), "last_seen": "2025-01-01", "reason": "quiet"},
        confidence=confidence,
        agent="staleness",
    )


def _service(name):
    return Node(
        node_type="service", name=name, properties={}, source_system="github", external_id=name
    )


def test_approve_all_threshold_and_errors(db, capsys):
    a = _service("svc-a")
    b = _service("svc-b")
    db.add_all([a, b])
    db.flush()
    db.add_all(
        [
            _stale_proposal(a.id, 0.95),
            _stale_proposal(a.id, 0.90),  # same node again -> handler rejects at apply
            _stale_proposal(b.id, 0.40),  # below the floor -> untouched
        ]
    )
    db.flush()

    rc = cmd_approve_all(db, 0.8, None, "tester")
    out = capsys.readouterr().out
    assert rc == 1
    assert "approved 1, failed 1, of 2 candidate(s)" in out
    db.refresh(a)
    assert a.properties.get("stale") is True
    low = db.query(Proposal).filter(Proposal.confidence == 0.40).one()
    assert low.status == "pending"


def test_approve_all_nothing_matching(db, capsys):
    rc = cmd_approve_all(db, 0.0, "no_such_kind", "tester")
    assert rc == 0
    assert "Nothing to approve" in capsys.readouterr().out
