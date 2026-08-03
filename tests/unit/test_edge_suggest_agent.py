"""Edge suggester: abstains offline, files a typed edge when the judge names one."""

from app.agents import AgentFactory
from app.agents.edge_suggest import EdgeSuggestion
from app.config import settings
from app.db.models import Edge, Node, NodeMention, RawDocument
from app.llm.stub import deterministic_embedding
from app.proposals import ProposalEngine


class FakeJudge:
    def __init__(self, suggestion):
        self.suggestion = suggestion

    def extract(self, system_prompt, user_content, schema):
        assert "allowed relationships:" in user_content
        return self.suggestion


def _seed_pair(db):
    """Two embedding-identical, well-mentioned nodes with no edge."""
    vec = deterministic_embedding("checkout incident", settings.embedding_dim)
    thread = Node(
        node_type="slack_thread",
        name="checkout outage thread",
        properties={},
        source_system="slack",
        external_id="t1",
        embedding=vec,
    )
    incident = Node(
        node_type="incident",
        name="checkout outage",
        properties={},
        source_system="github",
        external_id="i1",
        embedding=vec,
    )
    doc = RawDocument(source_system="slack", source_type="thread", external_id="d1", content="x")
    doc2 = RawDocument(source_system="github", source_type="issue", external_id="d2", content="y")
    db.add_all([thread, incident, doc, doc2])
    db.flush()
    for node in (thread, incident):
        db.add(NodeMention(node_id=node.id, document_id=doc.id))
        db.add(NodeMention(node_id=node.id, document_id=doc2.id))
    db.flush()
    return thread, incident


def test_stub_provider_abstains(db):
    _seed_pair(db)
    assert AgentFactory.create(db, "edge_suggest").scan() == []


def test_judge_suggestion_files_applies_and_rolls_back(db, monkeypatch):
    thread, incident = _seed_pair(db)
    suggestion = EdgeSuggestion(
        relationship="DISCUSSES",
        direction="a_to_b" if thread.id < incident.id else "b_to_a",
        confidence=0.85,
        rationale="The thread is the live discussion of the incident.",
    )
    monkeypatch.setattr("app.agents.edge_suggest.get_llm_provider", lambda: FakeJudge(suggestion))

    filed = AgentFactory.create(db, "edge_suggest").scan()
    assert len(filed) == 1
    proposal = filed[0]
    assert proposal.kind == "edge_add"
    assert proposal.payload["relationship"] == "DISCUSSES"
    assert proposal.payload["source_id"] == str(thread.id)
    assert proposal.evidence["llm_rationale"].startswith("The thread")

    # Sticky: the pair is never re-judged or re-filed.
    assert AgentFactory.create(db, "edge_suggest").scan() == []

    engine = ProposalEngine(db)
    engine.approve(proposal.id, reviewed_by="tester")
    edge = db.query(Edge).filter_by(source_id=thread.id, target_id=incident.id).one()
    assert edge.relationship_type == "DISCUSSES"
    assert edge.confidence == 0.85

    engine.rollback(proposal.id, reviewed_by="tester")
    assert db.query(Edge).filter_by(source_id=thread.id).count() == 0


def test_low_confidence_and_unknown_relationship_abstain(db, monkeypatch):
    _seed_pair(db)
    for suggestion in (
        EdgeSuggestion(relationship="DISCUSSES", confidence=0.2),
        EdgeSuggestion(relationship="MADE_UP_REL", confidence=0.9),
    ):
        monkeypatch.setattr(
            "app.agents.edge_suggest.get_llm_provider", lambda s=suggestion: FakeJudge(s)
        )
        assert AgentFactory.create(db, "edge_suggest").scan() == []
