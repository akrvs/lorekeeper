"""RBAC row-filtering: a scoped principal cannot see another source's nodes."""

from sqlalchemy import select

from app.db.models import Node
from app.repositories import GraphRepository
from app.security.principal import Principal, SourceGrant
from tests.fixtures import populate


def test_github_only_principal_cannot_see_slack_nodes(db, provider):
    populate(db, provider)
    feature = db.scalar(select(Node).where(Node.node_type == "feature"))
    thread = db.scalar(select(Node).where(Node.node_type == "slack_thread"))

    github_only = Principal(subject="eng-user", groups=("eng",), grants=(SourceGrant("github"),))
    repo = GraphRepository(db, github_only)

    # The feature is mentioned in a GitHub PR -> visible.
    assert repo.get_node(feature.id) is not None
    # The Slack thread only exists in a Slack document -> invisible.
    assert repo.get_node(thread.id) is None

    # Semantic search never surfaces the hidden slack_thread for this principal.
    qvec = provider.embed([f"{thread.name}\n{thread.summary}"])[0]
    rows = repo.semantic_search(qvec, None, 50)
    assert all(node.node_type != "slack_thread" for node, _ in rows)


def test_superuser_sees_everything(db, provider):
    populate(db, provider)
    thread = db.scalar(select(Node).where(Node.node_type == "slack_thread"))
    repo = GraphRepository(db, Principal.anonymous())
    assert repo.get_node(thread.id) is not None
