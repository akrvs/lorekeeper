"""Digest delivery: build the weekly digest and push it where humans look.

The digest itself is read-only (app.agents.digest); this module only moves the
finished text to a Slack-compatible incoming webhook when one is configured.
Run it from cron or wire it into the scheduler.

    python -m app.digest_delivery [--days N] [--webhook URL]
"""

from __future__ import annotations

import argparse
import logging
import sys

import httpx
from sqlalchemy.orm import Session

from app.agents.digest import build_digest
from app.config import settings

logger = logging.getLogger("company_brain.digest")


def post_slack(webhook_url: str, text: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Post ``text`` to a Slack-style incoming webhook. Returns (ok, detail)."""
    try:
        resp = httpx.post(webhook_url, json={"text": text}, timeout=timeout)
    except httpx.HTTPError as exc:
        return False, f"webhook unreachable: {exc}"
    if resp.is_success:
        return True, "delivered"
    return False, f"webhook returned HTTP {resp.status_code}"


def deliver(db: Session, days: int = 7, webhook_url: str | None = None) -> str:
    """Build the digest; deliver it when a webhook is available, else return it."""
    text = build_digest(db, days=days)
    target = webhook_url or settings.digest_webhook_url
    if not target:
        return text
    ok, detail = post_slack(target, text)
    if not ok:
        logger.warning("digest delivery failed: %s", detail)
        return f"Delivery failed ({detail}). Digest follows.\n\n{text}"
    logger.info("Digest delivered (%s)", detail)
    return "Digest delivered."


def _main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.digest_delivery",
        description="Build the graph-activity digest and deliver it to Slack.",
    )
    parser.add_argument("--days", type=int, default=None, help="Window in days.")
    parser.add_argument(
        "--webhook", default=None, help="Slack-compatible webhook (default DIGEST_WEBHOOK_URL)."
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        result = deliver(db, days=args.days or settings.digest_days, webhook_url=args.webhook)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
