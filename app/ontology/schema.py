"""Strict Pydantic schemas for LLM structured extraction.

The node/relationship type enums are generated *from the seeded ontology*
(`ontology_seed.NODE_TYPES` / `RELATIONSHIP_TYPES`), so the LLM is constrained
at the JSON-schema level to emit only terms the database knows about — the same
terms backed by FKs in `ontology_node_types` / `ontology_relationship_types`.
Extending the ontology automatically widens what the extractor may produce.

Why list[PropertyKV] instead of dict: OpenAI strict structured outputs forbid
open-ended objects (additionalProperties must be false), so dynamic key/value
maps are modelled as an explicit list and folded back into a JSONB dict by the
resolver.
"""

from enum import Enum
from functools import lru_cache as _lru_cache
from typing import Literal

from pydantic import BaseModel, Field, create_model

from app.db.ontology_seed import NODE_TYPES, RELATIONSHIP_TYPES

# --- Enums derived from the ontology registry ------------------------------
NodeTypeEnum = Enum(  # type: ignore[misc]
    "NodeTypeEnum",
    {nt["name"]: nt["name"] for nt in NODE_TYPES},
    type=str,
)
RelationshipTypeEnum = Enum(  # type: ignore[misc]
    "RelationshipTypeEnum",
    {rt["name"]: rt["name"] for rt in RELATIONSHIP_TYPES},
    type=str,
)


class PropertyKV(BaseModel):
    key: str
    value: str


class ExtractedNode(BaseModel):
    """One entity the LLM identified in a document."""

    temp_id: str = Field(
        description="Local id (e.g. 'n1') used to reference this node from edges in this payload."
    )
    node_type: NodeTypeEnum
    name: str = Field(description="Canonical human-readable name of the entity.")
    summary: str = Field(description="1-2 sentence description; this text is embedded.")
    # Sourced entities carry the source id; inferred/derived entities set both null.
    source_system: str | None = Field(
        default=None, description="Origin system if this is a concrete source artifact, else null."
    )
    external_id: str | None = Field(
        default=None, description="Id in the source system, else null for inferred entities."
    )
    properties: list[PropertyKV] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    def props_dict(self) -> dict[str, str]:
        return {p.key: p.value for p in self.properties}

    @property
    def is_sourced(self) -> bool:
        return bool(self.external_id and self.source_system)


class ExtractedEdge(BaseModel):
    """A directed relationship between two extracted nodes (by temp_id)."""

    source_temp_id: str
    target_temp_id: str
    relationship: RelationshipTypeEnum
    properties: list[PropertyKV] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    def props_dict(self) -> dict[str, str]:
        return {p.key: p.value for p in self.properties}


class UnmappedType(BaseModel):
    """The drift escape hatch: something important in the document does not fit
    any ontology term. Instead of forcing it into a wrong type (or silently
    dropping it), the model reports it here — the pipeline turns these into
    schema proposals a human can approve."""

    kind: Literal["node", "relationship"]
    name: str = Field(
        description="Proposed type name: snake_case for nodes, UPPER_SNAKE for relationships."
    )
    description: str = Field(description="One sentence: what this type means.")
    example: str = Field(description="The entity/relation in THIS document that needed it.")


class ExtractionResult(BaseModel):
    """The full structured payload returned for a single raw document."""

    nodes: list[ExtractedNode] = Field(default_factory=list)
    edges: list[ExtractedEdge] = Field(default_factory=list)
    unmapped_types: list[UnmappedType] = Field(default_factory=list)


# --- Live-registry (dynamic) extraction models ------------------------------
# The static enums above are frozen at import from the seed lists. Once schema
# proposals start extending the registry at runtime, extraction must follow the
# DATABASE's vocabulary — otherwise an approved type could never be extracted
# and would re-surface as drift forever.
@_lru_cache(maxsize=8)
def _build_extraction_model(node_names: tuple[str, ...], rel_names: tuple[str, ...]):
    node_enum = Enum("NodeTypeEnum", {n: n for n in node_names}, type=str)
    rel_enum = Enum("RelationshipTypeEnum", {r: r for r in rel_names}, type=str)
    node_model = create_model("ExtractedNode", __base__=ExtractedNode, node_type=(node_enum, ...))
    edge_model = create_model("ExtractedEdge", __base__=ExtractedEdge, relationship=(rel_enum, ...))
    return create_model(
        "ExtractionResult",
        __base__=ExtractionResult,
        nodes=(list[node_model], Field(default_factory=list)),
        edges=(list[edge_model], Field(default_factory=list)),
    )


def extraction_model_for(node_names: list[str], rel_names: list[str]) -> type[ExtractionResult]:
    """A strict ExtractionResult whose enums match the given vocabulary.
    Cached per vocabulary, so a registry INSERT yields a fresh model on the
    next extraction while unchanged vocabularies reuse the same class."""
    return _build_extraction_model(tuple(sorted(node_names)), tuple(sorted(rel_names)))
