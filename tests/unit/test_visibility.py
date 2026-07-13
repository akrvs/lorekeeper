"""Visibility predicate basics (the RBAC row filter)."""

from sqlalchemy import select

from app.db.models import Node
from app.security.principal import Principal, SourceGrant
from app.security.visibility import visible_node_ids


def test_superuser_is_unrestricted():
    assert visible_node_ids(Principal.anonymous()) is None


def test_no_grants_sees_nothing(db):
    principal = Principal(subject="u", grants=())
    rows = db.execute(select(Node.id).where(Node.id.in_(visible_node_ids(principal)))).all()
    assert rows == []


def test_grant_builds_a_subquery(db):
    principal = Principal(subject="u", grants=(SourceGrant("github", None),))
    # Should compile + execute without error (returns whatever is granted).
    assert visible_node_ids(principal) is not None
    db.execute(select(Node.id).where(Node.id.in_(visible_node_ids(principal)))).all()
