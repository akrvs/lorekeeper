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

from pydantic import BaseModel, Field

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


class ExtractionResult(BaseModel):
    """The full structured payload returned for a single raw document."""

    nodes: list[ExtractedNode] = Field(default_factory=list)
    edges: list[ExtractedEdge] = Field(default_factory=list)
