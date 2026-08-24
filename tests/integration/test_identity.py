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
    monkeypatch.setattr(
        "app.security.identity.settings.oidc_trust_gateway_tokens", True
    )
    db.add(AccessGrant(group_name="eng", source_system="github", resource_key=None))
    db.flush()

    principal = IdentityResolver().resolve(db, _jwt({"sub": "alice", "groups": ["eng"]}))
    assert principal.subject == "alice"
    assert principal.superuser is False
    assert SourceGrant("github", None) in principal.grants


def test_unverified_claims_refused_without_explicit_gateway_trust(db, monkeypatch):
    monkeypatch.setattr("app.security.identity.settings.rbac_enabled", True)
    with pytest.raises(PermissionError):
        IdentityResolver().resolve(db, _jwt({"sub": "alice", "groups": ["eng"]}))


def test_rbac_enabled_requires_a_token(db, monkeypatch):
    monkeypatch.setattr("app.security.identity.settings.rbac_enabled", True)
    with pytest.raises(PermissionError):
        IdentityResolver().resolve(db, None)


def test_rbac_disabled_is_anonymous_superuser(db):
    # Default settings (RBAC off) -> unrestricted, no token needed.
    principal = IdentityResolver().resolve(db, None)
    assert principal.superuser is True


def _jwks_resolver(monkeypatch, public_key):
    """An IdentityResolver whose JWKS client serves `public_key` (no network)."""
    monkeypatch.setattr("app.security.identity.settings.oidc_jwks_url", "https://idp/jwks")
    resolver = IdentityResolver()
    signing_key = type("SigningKey", (), {"key": public_key})()
    client = type("JWKSClient", (), {"get_signing_key_from_jwt": lambda self, t: signing_key})()
    resolver._jwks_client = client
    return resolver


def test_jwks_verifies_a_signed_token(monkeypatch):
    jwt = pytest.importorskip("jwt")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    resolver = _jwks_resolver(monkeypatch, private.public_key())

    from datetime import UTC, datetime, timedelta

    exp = datetime.now(UTC) + timedelta(hours=1)
    token = jwt.encode(
        {"sub": "alice", "groups": ["eng"], "exp": exp}, private, algorithm="RS256"
    )
    assert resolver._decode(token)["sub"] == "alice"


def test_jwks_rejects_a_forged_signature(monkeypatch):
    jwt = pytest.importorskip("jwt")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    trusted = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    resolver = _jwks_resolver(monkeypatch, trusted.public_key())

    forged = jwt.encode({"sub": "alice", "groups": ["admins"]}, attacker, algorithm="RS256")
    with pytest.raises(PermissionError):
        resolver._decode(forged)


def test_jwks_rejects_a_malformed_token(monkeypatch):
    pytest.importorskip("jwt")
    monkeypatch.setattr("app.security.identity.settings.oidc_jwks_url", "https://idp/jwks")
    with pytest.raises(PermissionError):
        IdentityResolver()._decode("not-a-jwt")
