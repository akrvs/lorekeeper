"""Ingestion + extraction pipeline (glue across the three layers) + CLI.

    connector.run()                -> raw_documents
    extractor.extract_document()   -> ExtractionResult (Azure structured output)
    embeddings.embed_extraction()  -> {temp_id: vector}
    resolver.resolve_document()    -> nodes / edges / mentions

End-to-end idempotent: connectors upsert on natural keys, the resolver
upserts/dedups, so re-running a source is safe.

CLI (turnkey live sync):
    python -m app.pipeline --source github --repo owner/name
    python -m app.pipeline --source slack  --channel C0123456789
"""

import json
import logging
import sys

from sqlalchemy.orm import Session

from app.cache import get_cache
from app.config import settings
from app.connectors import ConnectorError, ConnectorFactory
from app.connectors.base import BaseConnector
from app.db.models.source import RawDocument
from app.llm import get_llm_provider
from app.llm.base import LLMProvider
from app.ontology.embeddings import collect_embed_items, embed_extraction, embed_many
from app.ontology.extractor import extract_document
from app.ontology.resolver import Resolver, ResolveStats
from app.proposals import file_unmapped

logger = logging.getLogger("company_brain.pipeline")


def _build_connector(
    db: Session, source: str, *, repo: str | None, channel_id: str | None, limit: int | None
) -> BaseConnector:
    # The factory passes overrides through; each connector ignores ones it
    # doesn't use and falls back to settings for unsupplied config.
    return ConnectorFactory.create(db, source, repo=repo, channel_id=channel_id, max_items=limit)


def process_document(db: Session, provider: LLMProvider, document: RawDocument) -> ResolveStats:
    extraction = extract_document(provider, document, db)
    file_unmapped(db, document, extraction.unmapped_types)  # drift -> schema proposals
    embeddings = embed_extraction(provider, extraction)
    return Resolver(db).resolve_document(document, extraction, embeddings)


def run_source(
    db: Session,
    source: str,
    *,
    repo: str | None = None,
    channel_id: str | None = None,
    limit: int | None = None,
    provider: LLMProvider | None = None,
) -> dict:
    """Ingest a source end-to-end and return an aggregate report."""
    provider = provider or get_llm_provider()
    connector = _build_connector(db, source, repo=repo, channel_id=channel_id, limit=limit)

    run, documents = connector.run()

    # Track 3: extract every document, batch-embed all node texts in one pass,
    # then resolve — collapsing N per-document embedding calls into ⌈N/size⌉.
    extractions = []
    embed_items: list = []
    drift_filed = 0
    for doc in documents:
        extraction = extract_document(provider, doc, db)
        drift_filed += len(file_unmapped(db, doc, extraction.unmapped_types))
        extractions.append((doc, extraction))
        embed_items.extend(collect_embed_items(doc.id, extraction))
    vectors = embed_many(provider, embed_items, settings.embedding_batch_size)

    totals = {"nodes_created": 0, "nodes_updated": 0, "nodes_merged": 0, "edges_upserted": 0}
    for doc, extraction in extractions:
        embeddings = {n.temp_id: vectors[(doc.id, n.temp_id)] for n in extraction.nodes}
        stats = Resolver(db).resolve_document(doc, extraction, embeddings)
        for key in totals:
            totals[key] += getattr(stats, key)

    if source == "local":
        # Resolve [[wikilinks]] to real file nodes (Phase 9).
        from app.connectors.local import reconcile_wikilinks

        totals["wikilinks_resolved"] = reconcile_wikilinks(db)

    get_cache().bump("graph")  # invalidate cached reads after a sync

    report = {
        "source": source,
        "target": repo or channel_id or settings.github_repo or settings.slack_channel_id,
        "ingestion_run_id": str(run.id),
        "documents": len(documents),
        "schema_drift_proposals": drift_filed,
        **totals,
    }
    logger.info("Pipeline report: %s", report)
    return report


def _main() -> int:
    import argparse

    from app.db.init_db import init_db
    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline",
        description="Run a live ingestion sync into the Company Brain graph.",
    )
    parser.add_argument("--source", required=True, choices=ConnectorFactory.available())
    parser.add_argument("--repo", help="GitHub repository as 'owner/name'.")
    parser.add_argument("--channel", dest="channel_id", help="Slack channel id, e.g. C0123456789.")
    parser.add_argument("--limit", type=int, default=None, help="Max items to pull this run.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    init_db()  # idempotent: ensures schema + ontology exist
    try:
        with SessionLocal() as db:
            report = run_source(
                db, args.source, repo=args.repo, channel_id=args.channel_id, limit=args.limit
            )
    except ValueError as exc:  # misconfiguration: missing token/repo/channel
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ConnectorError as exc:  # upstream API failure after retries
        print(f"error: upstream API failure — {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
