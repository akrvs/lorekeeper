"""Locally minted, short-lived principal tokens (HS256).

For teams that run without an OIDC identity provider but still want RBAC:
an operator with LOCAL_TOKEN_SIGNING_KEY can mint expiring tokens bound to
IdP-style groups, and IdentityResolver verifies them against the same key.
Signature and expiry are always enforced - these are never "trust me" tokens.

    python -m app.security.local_tokens --subject alice --group eng --hours 8
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from app.config import settings


def mint(
    subject: str,
    groups: list[str],
    *,
    ttl_hours: float = 8.0,
    key: str | None = None,
) -> str:
    import jwt

    signing_key = key or settings.local_token_signing_key
    if not signing_key:
        raise RuntimeError("Set LOCAL_TOKEN_SIGNING_KEY before minting local tokens.")
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "groups": groups,
        "iat": now,
        "exp": now + timedelta(hours=ttl_hours),
        "iss": "lorekeeper-local",
    }
    return jwt.encode(payload, signing_key, algorithm="HS256")


def decode_local(token: str, *, key: str | None = None) -> dict:
    """Verify a locally minted token. Raises PermissionError on any failure."""
    import jwt

    signing_key = key or settings.local_token_signing_key
    if not signing_key:
        raise PermissionError("LOCAL_TOKEN_SIGNING_KEY is not configured.")
    try:
        return jwt.decode(
            token,
            signing_key,
            algorithms=["HS256"],
            issuer="lorekeeper-local",
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise PermissionError(f"Local token rejected: {exc}") from exc


def looks_like_local_token(token: str) -> bool:
    """Cheap header check: HS256 tokens carry alg=HS256 in their JWT header."""
    from app.security.identity import _b64url_json

    try:
        header = _b64url_json(token.split(".")[0])
    except Exception:
        return False
    return isinstance(header, dict) and header.get("alg") == "HS256"


def _main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.security.local_tokens",
        description="Mint a short-lived RBAC principal token (HS256).",
    )
    parser.add_argument("--subject", required=True, help="Principal id (e.g. a username).")
    parser.add_argument(
        "--group", action="append", default=[], dest="groups", help="IdP group (repeatable)."
    )
    parser.add_argument("--hours", type=float, default=8.0, help="Token lifetime (default 8h).")
    args = parser.parse_args()

    try:
        token = mint(args.subject, args.groups, ttl_hours=args.hours)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
