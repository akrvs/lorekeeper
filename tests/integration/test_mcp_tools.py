"""The four MCP tools over a committed graph (RBAC disabled → unrestricted)."""

from sqlalchemy import select

from app.db.models import Node
from app.db.session import SessionLocal
from app.mcp.server import (
    ask,
    export_subgraph,
    get_entity_timeline,
    get_graph_stats,
    get_node_details,
    get_node_neighbors,
    semantic_search,
    traverse_graph_path,
)


def _nodes():
    with SessionLocal() as db:
        feature = db.scalar(select(Node).where(Node.node_type == "feature"))
        repo = db.scalar(select(Node).where(Node.node_type == "repository"))
    return feature, repo


def test_semantic_search(committed_graph):
    feature, _ = _nodes()
    res = semantic_search(f"{feature.name}\n{feature.summary}", node_type="feature", limit=3)
    assert "checkout-v2" in res and "similarity:" in res


def test_get_node_details_lists_both_sources(committed_graph):
    feature, _ = _nodes()
    res = get_node_details(str(feature.id))
    assert "github" in res and "slack.com/archives" in res


def test_get_node_neighbors(committed_graph):
    feature, _ = _nodes()
    res = get_node_neighbors(str(feature.id), direction="both")
    assert "IMPLEMENTS" in res and "DISCUSSES" in res


def test_traverse_repo_to_slack_thread(committed_graph):
    _, repo = _nodes()
    res = traverse_graph_path(str(repo.id), "slack_thread", max_depth=4)
    assert "slack_thread" in res


def test_invalid_uuid_is_rejected(committed_graph):
    assert "not a valid UUID" in get_node_details("not-a-uuid")


def test_entity_timeline_lists_dated_events(committed_graph):
    feature, _ = _nodes()
    res = get_entity_timeline(str(feature.id))
    assert "TIMELINE" in res and "2026-05-20" in res and "github" in res


def test_entity_timeline_rejects_bad_uuid(committed_graph):
    assert "not a valid UUID" in get_entity_timeline("nope")


def test_ask_returns_entities_connections_and_sources(committed_graph):
    feature, _ = _nodes()
    res = ask(f"{feature.name}\n{feature.summary}", limit=2)
    assert "ANSWER CONTEXT" in res
    assert "checkout-v2" in res
    assert "SOURCES (" in res and "github" in res
    assert "-[" in res


def test_export_subgraph_returns_mermaid(committed_graph):
    feature, _ = _nodes()
    res = export_subgraph(str(feature.id), depth=2)
    assert res.startswith("graph LR")
    assert "-->|IMPLEMENTS|" in res
    assert "(feature)" in res and "(repository)" in res


def test_export_subgraph_rejects_bad_uuid(committed_graph):
    assert "not a valid UUID" in export_subgraph("nah")


def test_graph_stats_reports_counts_and_syncs(committed_graph):
    res = get_graph_stats()
    assert "GRAPH STATS" in res
    assert "NODES BY TYPE:" in res and "feature:" in res
    assert "EDGES BY RELATIONSHIP:" in res
    assert "LAST COMPLETED SYNC PER SOURCE:" in res and "github:" in res
