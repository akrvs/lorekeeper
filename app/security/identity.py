"""Token -> Principal resolution.

Behavior:
  * RBAC disabled  -> Principal.anonymous() (superuser); fully back-compatible.
  * RBAC enabled   -> decode the JWT claims, read the caller's groups, and map
    them to grants via the `access_grants` table.

With `OIDC_JWKS_URL` set the JWT signature is verified against the IdP's JWKS
(plus audience/issuer when `OIDC_AUDIENCE`/`OIDC_ISSUER` are configured).
Without it the claims are decoded unverified — that path refuses to run unless
`OIDC_TRUST_GATEWAY_TOKENS=true`, which says your own gateway has already
verified the token before injecting it.
"""

import base64
import binascii
import json
import logging
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models.security import AccessGrant
from app.security.principal import Principal, SourceGrant

logger = logging.getLogger("company_brain.security.identity")


def _b64url_json(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def grants_for_groups(db: Session, groups: tuple[str, ...]) -> tuple[SourceGrant, ...]:
    """Resolve the union of grants for a set of IdP groups from the policy table."""
    if not groups:
        return ()
    rows = db.execute(
        select(AccessGrant.source_system, AccessGrant.resource_key).where(
            AccessGrant.group_name.in_(groups)
        )
    ).all()
    wildcard: set[str] = set()  # sources granted in full
    specific: dict[str, set[str]] = {}  # source -> explicit resource keys
    for source_system, resource_key in rows:
        if resource_key is None:
            wildcard.add(source_system)
        else:
            specific.setdefault(source_system, set()).add(resource_key)
    grants = [SourceGrant(s, None) for s in wildcard]
    grants += [SourceGrant(s, frozenset(keys)) for s, keys in specific.items() if s not in wildcard]
    return tuple(grants)


class IdentityResolver:
    def __init__(self) -> None:
        self._jwks_client = None

    def resolve(self, db: Session, token: str | None) -> Principal:
        if not settings.rbac_enabled:
            return Principal.anonymous()
        if not token:
            raise PermissionError("RBAC enabled but no principal token was supplied.")
        claims = self._decode(token)
        subject = str(claims.get("sub") or claims.get("oid") or claims.get("email") or "unknown")
        groups = tuple(claims.get("groups") or claims.get("roles") or ())
        return Principal(subject=subject, groups=groups, grants=grants_for_groups(db, groups))

    def _decode(self, token: str) -> dict:
        if settings.oidc_jwks_url:
            return self._decode_verified(token)
        if not settings.oidc_trust_gateway_tokens:
            raise PermissionError(
                "RBAC is enabled without OIDC_JWKS_URL, so tokens cannot be "
                "verified. Set OIDC_JWKS_URL, or set "
                "OIDC_TRUST_GATEWAY_TOKENS=true only if your gateway verifies "
                "the token before injecting it."
            )
        try:
            return _b64url_json(token.split(".")[1])
        except (IndexError, ValueError, binascii.Error) as exc:
            raise PermissionError(f"Malformed principal token: {exc}") from exc

    def _decode_verified(self, token: str) -> dict:
        try:
            import jwt  # noqa: PLC0415 — optional dependency, imported lazily
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "OIDC_JWKS_URL requires the 'PyJWT' package (pip install 'PyJWT[crypto]')."
            ) from exc
        if self._jwks_client is None:
            self._jwks_client = jwt.PyJWKClient(settings.oidc_jwks_url)
        try:
            key = self._jwks_client.get_signing_key_from_jwt(token).key
            return jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                audience=settings.oidc_audience,
                issuer=settings.oidc_issuer,
                options={
                    "verify_aud": settings.oidc_audience is not None,
                    "verify_iss": settings.oidc_issuer is not None,
                    "require": ["exp", "sub"],
                },
            )
        except jwt.PyJWTError as exc:
            raise PermissionError(f"JWT verification failed: {exc}") from exc


@lru_cache
def get_identity_resolver() -> IdentityResolver:
    return IdentityResolver()
