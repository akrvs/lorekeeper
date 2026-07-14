"""Ontology bootstrap — ask the LLM what vocabulary the ontology is missing.

    python -m app.bootstrap [--sample 25]

Where drift detection catches missing types one document at a time during
ingestion, bootstrap sweeps a sample of ALREADY-INGESTED documents in one pass —
the move you make right after connecting a new company's tools, before trusting
the graph. Findings are filed as the same `schema_node_type` /
`schema_relationship_type` proposals drift produces; nothing is added to the
ontology until a human approves it in the review queue.

Offline note: the stub provider deliberately reports no findings (it has no
judgment); bootstrap is only meaningful with a real LLM provider.
"""

import json
import logging
import sys

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models.source import RawDocument
from app.llm import get_llm_provider
from app.llm.base import LLMProvider
from app.ontology.extractor import build_system_prompt
from app.ontology.registry import live_registry
from app.ontology.schema import UnmappedType
from app.proposals import file_unmapped
from app.textutil import clip_text

logger = logging.getLogger("company_brain.bootstrap")


class BootstrapFindings(BaseModel):
    """Missing-vocabulary report for one sampled document."""

    missing_types: list[UnmappedType] = Field(default_factory=list)


def _bootstrap_prompt(db: Session) -> str:
    node_types, rel_types = live_registry(db)
    ontology = build_system_prompt(node_types, rel_types)
    return (
        f"{ontology}\n\n"
        "BOOTSTRAP MODE: do NOT extract nodes or edges. Your only job is to audit "
        "the ontology against this document. Report every RECURRING kind of entity "
        "or relationship the document expresses that has no adequate type above — "
        "as missing_types (snake_case names for node types, UPPER_SNAKE for "
        "relationships). If the ontology already covers everything, return an "
        "empty list. Propose categories, not instances."
    )


def run_bootstrap(db: Session, provider: LLMProvider | None = None, sample: int | None = None):
    provider = provider or get_llm_provider()
    sample = sample or settings.bootstrap_sample_size
    docs = db.scalars(
        select(RawDocument).order_by(RawDocument.created_at.desc()).limit(sample)
    ).all()

    prompt = _bootstrap_prompt(db)
    filed = []
    for doc in docs:
        content = clip_text(doc.content, settings.extraction_max_chars)
        findings = provider.extract(
            prompt, f"title: {doc.title or '(untitled)'}\n---\n{content}", BootstrapFindings
        )
        filed.extend(file_unmapped(db, doc, findings.missing_types, agent="bootstrap"))

    return {
        "documents_sampled": len(docs),
        "proposals_filed": len(filed),
        "proposed": [f"{p.kind}:{p.payload['name']}" for p in filed],
    }


def _main() -> int:
    import argparse

    from app.db.init_db import init_db
    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser(
        prog="python -m app.bootstrap",
        description="Sample ingested documents and propose missing ontology types.",
    )
    parser.add_argument("--sample", type=int, default=None, help="Documents to sample.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    init_db()
    with SessionLocal() as db:
        report = run_bootstrap(db, sample=args.sample)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
