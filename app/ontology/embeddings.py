"""Embedding generation for extracted nodes.

Each node's embedded text is `name + summary` — the canonical, human-readable
representation used both for semantic search (MCP) and for the semantic half of
entity dedup.

`embed_extraction` embeds one document. `embed_many` (Track 3) pools texts across
a whole ingestion batch and flushes them to the provider in chunks of
`EMBEDDING_BATCH_SIZE`, collapsing N per-document round-trips into ⌈N/size⌉.
"""

from collections.abc import Hashable

from app.llm.base import LLMProvider
from app.ontology.schema import ExtractionResult


def _node_text(name: str, summary: str) -> str:
    return f"{name}\n{summary}".strip()


def _batched(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def embed_extraction(provider: LLMProvider, extraction: ExtractionResult) -> dict[str, list[float]]:
    """Return {temp_id: embedding} for every node in a single extraction."""
    if not extraction.nodes:
        return {}
    texts = [_node_text(n.name, n.summary) for n in extraction.nodes]
    vectors = provider.embed(texts)
    return {node.temp_id: vec for node, vec in zip(extraction.nodes, vectors, strict=True)}


def embed_many(
    provider: LLMProvider, items: list[tuple[Hashable, str]], batch_size: int
) -> dict[Hashable, list[float]]:
    """Batch-embed `(key, text)` pairs; one provider call per `batch_size` items."""
    out: dict[Hashable, list[float]] = {}
    for chunk in _batched(items, max(1, batch_size)):
        vectors = provider.embed([text for _, text in chunk])
        out.update({key: vec for (key, _), vec in zip(chunk, vectors, strict=True)})
    return out


def collect_embed_items(
    doc_key: Hashable, extraction: ExtractionResult
) -> list[tuple[Hashable, str]]:
    """Build globally-unique `((doc_key, temp_id), text)` pairs for batching."""
    return [((doc_key, n.temp_id), _node_text(n.name, n.summary)) for n in extraction.nodes]
