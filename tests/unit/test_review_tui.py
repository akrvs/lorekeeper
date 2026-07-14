"""Review TUI plumbing — queue loading and row/detail rendering are plain
data-in/data-out, so they get tested without a terminal (curses stays untouched)."""

from app.db.models import Node
from app.proposals import ProposalEngine
from app.review_tui import _detail_lines, load_queue, row_text


def _merge_proposal(db):
    a = Node(node_type="user", name="alice", properties={}, source_system="github", external_id="a")
    b = Node(
        node_type="user", name="alice", properties={}, source_system="slack", external_id="U_A"
    )
    db.add_all([a, b])
    db.flush()
    return ProposalEngine(db).submit(
        "entity_merge",
        {"loser_id": str(b.id), "winner_id": str(a.id), "reason": "dedup: looks the same"},
        confidence=0.91,
        agent="dedup",
        evidence={
            "winner": {"name": "alice", "source": "github"},
            "loser": {"name": "alice", "source": "slack"},
            "llm_judgment": {"verdict": "same", "confidence": 0.95, "rationale": "One human."},
        },
    )


def test_load_queue_filters_by_status(db):
    p = _merge_proposal(db)
    assert [q.id for q in load_queue(db, "pending")] == [p.id]
    assert load_queue(db, "applied") == []
    assert [q.id for q in load_queue(db, None)] == [p.id]  # None == every status


def test_row_text_is_one_scannable_line(db):
    p = _merge_proposal(db)
    row = row_text(p)
    assert str(p.id)[:8] in row
    assert "entity_merge" in row and "0.91" in row and "merge 'alice' -> 'alice'" in row


def test_detail_lines_surface_judgment_and_lifecycle(db):
    p = _merge_proposal(db)
    detail = "\n".join(_detail_lines(p))
    assert f"proposal   {p.id}" in detail
    assert "confidence 0.910" in detail
    # The judge's verdict gets an above-the-fold line (JSONB reorders evidence keys)
    assert "judge      same (0.95) — One human." in detail
    assert '"rationale": "One human."' in detail  # ...and stays in the raw evidence

    ProposalEngine(db).approve(p.id, reviewed_by="tester")
    detail = "\n".join(_detail_lines(p))
    assert "reviewed   by tester" in detail
    assert "rollback   snapshot available" in detail
