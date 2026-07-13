"""RBAC identity flow: JWT claims -> groups -> grants (via access_grants)."""

import base64
import json

import pytest

from app.db.models.security import AccessGrant
from app.security.identity import IdentityResolver
from app.security.principal import SourceGrant


def _jwt(payload: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{seg}.signature"


def test_token_resolves_groups_to_grants(db, monkeypatch):
    monkeypatch.setattr("app.security.identity.settings.rbac_enabled", True)
    db.add(AccessGrant(group_name="eng", source_system="github", resource_key=None))
    db.flush()

    principal = IdentityResolver().resolve(db, _jwt({"sub": "alice", "groups": ["eng"]}))
    assert principal.subject == "alice"
    assert principal.superuser is False
    assert SourceGrant("github", None) in principal.grants


def test_rbac_enabled_requires_a_token(db, monkeypatch):
    monkeypatch.setattr("app.security.identity.settings.rbac_enabled", True)
    with pytest.raises(PermissionError):
        IdentityResolver().resolve(db, None)


def test_rbac_disabled_is_anonymous_superuser(db):
    # Default settings (RBAC off) -> unrestricted, no token needed.
    principal = IdentityResolver().resolve(db, None)
    assert principal.superuser is True
