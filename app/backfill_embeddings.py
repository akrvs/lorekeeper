"""Re-embed the graph when the embedding model changes.

Switching embedding models silently mixes vector spaces: old nodes and new
nodes stop being comparable and semantic search degrades. This backfill
re-embeds every node with the currently configured provider, in batches, and
stamps the model name into node properties so future runs can detect drift.

    python -m app.backfill_embeddings [--dry-run] [--limit N] [--batch-size N]
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from app.db.init_db import init_db
from app.db.models import Node
from app.llm import get_llm_provider

logger = logging.getLogger("company_brain.backfill")

_EMBED_MODEL_KEY = "embedding_model"


def _embed_text(node: Node) -> str:
    return f"{node.name}\n{node.summary or ''}".strip()


def _provider_model_name(provider) -> str:
    return str(getattr(provider, "model", None) or getattr(provider, "name", "unknown"))


def plan_backfill(db, limit: int | None = None, model_name: str | None = None) -> list[Node]:
    """Nodes needing re-embedding: no stamp yet, or stamped by another model."""
    stmt = select(Node).order_by(Node.created_at)
    stamped = Node.properties.op("->>")(_EMBED_MODEL_KEY)
    if model_name:
        stmt = stmt.where(stamped.is_(None) | (stamped != model_name))
    else:
        stmt = stmt.where(stamped.is_(None))
    if limit:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def _stamp(node: Node, model_name: str) -> None:
    props = dict(node.properties or {})
    props[_EMBED_MODEL_KEY] = model_name
    node.properties = props


def run_backfill(db, *, batch_size: int, limit: int | None, dry_run: bool) -> dict:
    provider = get_llm_provider()
    model_name = _provider_model_name(provider)
    nodes = plan_backfill(db, limit=limit)
    report = {"model": model_name, "planned": len(nodes), "embedded": 0, "batches": 0}
    if dry_run:
        report["dry_run"] = True
        return report

    for start in range(0, len(nodes), batch_size):
        chunk = nodes[start : start + batch_size]
        vectors = provider.embed([_embed_text(n) for n in chunk])
        for node, vec in zip(chunk, vectors, strict=False):
            node.embedding = vec
            _stamp(node, model_name)
        db.commit()
        report["embedded"] += len(chunk)
        report["batches"] += 1
        logger.info("embedded %d/%d", report["embedded"], len(nodes))
    return report


def _main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.backfill_embeddings",
        description="Re-embed every node with the current provider.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Count targets without embedding.")
    parser.add_argument("--limit", type=int, default=None, help="Cap how many nodes to touch.")
    parser.add_argument("--batch-size", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    from app.config import settings
    from app.db.session import SessionLocal

    init_db()
    with SessionLocal() as db:
        report = run_backfill(
            db,
            batch_size=args.batch_size or settings.embedding_batch_size,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
