"""Token -> Principal resolution.

MVP behavior:
  * RBAC disabled  -> Principal.anonymous() (superuser); fully back-compatible.
  * RBAC enabled   -> decode the JWT claims, read the caller's groups, and map
    them to grants via the `access_grants` table.

The JWT is decoded WITHOUT signature verification here to avoid a hard PyJWT/
JWKS dependency in the open-source core. Production deployments should subclass
`IdentityResolver._decode` to verify against `OIDC_JWKS_URL`/`OIDC_AUDIENCE`.
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
        """Override in production to verify the signature against JWKS."""
        try:
            return _b64url_json(token.split(".")[1])
        except (IndexError, ValueError, binascii.Error) as exc:
            raise PermissionError(f"Malformed principal token: {exc}") from exc


@lru_cache
def get_identity_resolver() -> IdentityResolver:
    return IdentityResolver()
