"""Deterministic offline provider (LLM_PROVIDER=stub).

Lets the whole stack run without Azure credentials. Embeddings are deterministic
functions of the input text (identical text -> identical vector). `extract()`
does a lightweight, deterministic structured extraction so the offline path is
demoable end-to-end (a document node per artifact + `[[wikilink]]` references as
REFERENCES edges). It is NOT a substitute for the Azure structured extractor —
with LLM_PROVIDER=azure the same pipeline produces far richer entities/edges.
"""

import hashlib
import math
import random
import re
from typing import TypeVar

from pydantic import BaseModel

from app.config import settings

T = TypeVar("T", bound=BaseModel)

_FIELD = "^{name}: (.+)$"
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")


def deterministic_embedding(text: str, dim: int) -> list[float]:
    """A stable, L2-normalized pseudo-vector seeded by the text hash."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    vec = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class StubProvider:
    def __init__(self, dim: int | None = None):
        self._dim = dim or settings.embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [deterministic_embedding(t, self._dim) for t in texts]

    def extract(self, system_prompt: str, user_content: str, schema: type[T]) -> T:
        if "nodes" not in schema.model_fields:
            # Not an extraction payload (e.g. the bootstrap missing-type report):
            # the offline stub has no opinion — return the schema's empty default.
            return schema()

        def field(name: str) -> str | None:
            match = re.search(_FIELD.format(name=name), user_content, re.M)
            return match.group(1).strip() if match else None

        source_system = field("source_system")
        external_id = field("external_id")
        title = field("title") or external_id or "document"
        body = user_content.split("\n---\n", 1)[-1]

        # The artifact itself -> a sourced document node. Built as plain dicts
        # so the payload validates against BOTH the static schema and the
        # dynamic live-registry models (their enums coerce from strings).
        nodes: list[dict] = [
            {
                "temp_id": "d0",
                "node_type": "document",
                "name": title,
                "summary": (re.sub(r"\s+", " ", body).strip()[:160] or title),
                "source_system": source_system,
                "external_id": external_id,
                "properties": [],
                "confidence": 1.0,
            }
        ]
        edges: list[dict] = []

        # [[wikilinks]] -> derived 'document' concept nodes + REFERENCES edges.
        seen: list[str] = []
        for raw in _WIKILINK.findall(body):
            name = raw.strip()
            if name and name not in seen:
                seen.append(name)
        for i, name in enumerate(seen, start=1):
            tid = f"w{i}"
            nodes.append(
                {
                    "temp_id": tid,
                    "node_type": "document",
                    "name": name,
                    "summary": f"Note referenced via wikilink: {name}",
                    "source_system": None,  # derived -> deduped across files by name
                    "external_id": None,
                    "properties": [],
                    "confidence": 0.8,
                }
            )
            edges.append(
                {
                    "source_temp_id": "d0",
                    "target_temp_id": tid,
                    "relationship": "REFERENCES",
                    "properties": [],
                    "confidence": 0.8,
                }
            )
        return schema.model_validate({"nodes": nodes, "edges": edges})
