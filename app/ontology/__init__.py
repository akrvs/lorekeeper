"""Ontology engine — raw documents -> ontology-backed nodes and edges.

Modules:
    schema.py      — strict Pydantic structures for LLM structured output
    extractor.py   — ontology-aware prompt + provider.extract() -> ExtractionResult
    embeddings.py  — Azure OpenAI embedding generation for node text
    resolver.py    — idempotent upsert + entity dedup (trigram + pgvector) + provenance
"""

from app.ontology.embeddings import embed_extraction
from app.ontology.extractor import extract_document
from app.ontology.resolver import Resolver, ResolveStats
from app.ontology.schema import (
    ExtractedEdge,
    ExtractedNode,
    ExtractionResult,
    NodeTypeEnum,
    RelationshipTypeEnum,
)

__all__ = [
    "embed_extraction",
    "extract_document",
    "Resolver",
    "ResolveStats",
    "ExtractedNode",
    "ExtractedEdge",
    "ExtractionResult",
    "NodeTypeEnum",
    "RelationshipTypeEnum",
]
