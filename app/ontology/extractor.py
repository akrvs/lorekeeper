"""Document -> ExtractionResult, via the LLM provider.

The system prompt is assembled dynamically from the ontology registry so the
model always sees the current controlled vocabulary (type names, descriptions,
and the legal endpoints for each relationship).
"""

import logging

from app.config import settings
from app.db.models.source import RawDocument
from app.db.ontology_seed import NODE_TYPES, RELATIONSHIP_TYPES
from app.llm.base import LLMProvider
from app.ontology.schema import ExtractionResult
from app.textutil import clip_text

logger = logging.getLogger("company_brain.ontology.extractor")


def _format_endpoints(allowed: list | None) -> str:
    return "any" if not allowed else ", ".join(allowed)


def build_system_prompt() -> str:
    node_lines = "\n".join(f"  - {nt['name']}: {nt['description']}" for nt in NODE_TYPES)
    edge_lines = "\n".join(
        f"  - {rt['name']}: {rt['description']} "
        f"(source: {_format_endpoints(rt.get('allowed_source_types'))} -> "
        f"target: {_format_endpoints(rt.get('allowed_target_types'))})"
        for rt in RELATIONSHIP_TYPES
    )
    return (
        "You are the extraction engine of Company Brain, an organizational knowledge "
        "graph. Read a single source document and extract the entities (nodes) and the "
        "relationships (edges) it expresses, using ONLY the ontology below.\n\n"
        "NODE TYPES:\n"
        f"{node_lines}\n\n"
        "RELATIONSHIP TYPES (respect the allowed source/target types):\n"
        f"{edge_lines}\n\n"
        "RULES:\n"
        "1. Give every node a unique temp_id (n1, n2, ...) and reference those ids in edges.\n"
        "2. For entities that ARE the document or are concrete artifacts of the source "
        "system (a PR, an issue, a Slack thread, a user account), set source_system and "
        "external_id to the real identifiers.\n"
        "3. For abstract/inferred concepts (a feature, an incident/outage, a service) that "
        "have no id in the source system, leave source_system and external_id null — these "
        "will be de-duplicated across documents by name and meaning.\n"
        "4. Keep `name` canonical and stable (e.g. a feature called 'checkout-v2' should "
        "be named identically everywhere) so cross-document dedup works.\n"
        "5. summary is one or two sentences; it is embedded for semantic search.\n"
        "6. Only assert relationships actually supported by the text. Do not invent."
    )


def _render_document(doc: RawDocument) -> str:
    parts = [
        f"source_system: {doc.source_system}",
        f"source_type: {doc.source_type}",
        f"external_id: {doc.external_id}",
    ]
    if doc.title:
        parts.append(f"title: {doc.title}")
    if doc.author:
        parts.append(f"author: {doc.author}")
    if doc.url:
        parts.append(f"url: {doc.url}")
    parts.append("---")
    # Clip the body to the LLM budget so large PRs/threads don't blow the token
    # limit. The full text remains intact in raw_documents for provenance.
    parts.append(clip_text(doc.content, settings.extraction_max_chars))
    return "\n".join(parts)


def extract_document(provider: LLMProvider, doc: RawDocument) -> ExtractionResult:
    result = provider.extract(build_system_prompt(), _render_document(doc), ExtractionResult)
    logger.info(
        "Extracted %d nodes / %d edges from %s:%s",
        len(result.nodes),
        len(result.edges),
        doc.source_system,
        doc.external_id,
    )
    return result
