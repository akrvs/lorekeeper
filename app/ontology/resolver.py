"""Graph entity resolver — turns a validated ExtractionResult into graph rows.

Responsibilities:
  1. Upsert nodes, handling two regimes:
       a) SOURCED   (external_id + source_system present): idempotent upsert on
          the natural key uq_nodes_identity (node_type, source_system, external_id).
       b) DERIVED   (inferred concepts, no external_id): deduplicate against
          existing derived nodes using the pg_trgm name index (lexical) and the
          pgvector HNSW index (semantic) before inserting.
  2. Record provenance in node_mentions (node <- document).
  3. Upsert edges, idempotent on uq_edges_identity, tagged with the evidence
     document they were inferred from.

Everything for one document runs in a single transaction (committed by caller
or by resolve_document).
"""

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, literal_column, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.edge import Edge
from app.db.models.mention import NodeMention
from app.db.models.node import Node
from app.db.models.source import RawDocument
from app.ontology.schema import ExtractedEdge, ExtractedNode, ExtractionResult

logger = logging.getLogger("company_brain.ontology.resolver")


@dataclass
class ResolveStats:
    nodes_created: int = 0
    nodes_updated: int = 0
    nodes_merged: int = 0  # derived node matched an existing one
    edges_upserted: int = 0
    mentions: int = 0
    temp_to_id: dict[str, uuid.UUID] = field(default_factory=dict)


class Resolver:
    # pg_trgm similarity(name_a, name_b) at/above which two names are "the same".
    NAME_SIM_THRESHOLD = 0.55
    # cosine distance (1 - cosine similarity); <= this counts as semantically same.
    VEC_DIST_THRESHOLD = 0.15

    def __init__(self, db: Session):
        self.db = db

    # -- public ------------------------------------------------------------
    def resolve_document(
        self,
        document: RawDocument,
        extraction: ExtractionResult,
        embeddings: dict[str, list[float]],
    ) -> ResolveStats:
        stats = ResolveStats()

        for node in extraction.nodes:
            vec = embeddings.get(node.temp_id)
            node_id = self._resolve_node(node, vec, stats)
            stats.temp_to_id[node.temp_id] = node_id
            if self._record_mention(node_id, document.id, node.summary):
                stats.mentions += 1

        for edge in extraction.edges:
            if self._resolve_edge(edge, document.id, stats.temp_to_id):
                stats.edges_upserted += 1

        self.db.commit()
        logger.info(
            "Resolved %s:%s -> created=%d updated=%d merged=%d edges=%d",
            document.source_system,
            document.external_id,
            stats.nodes_created,
            stats.nodes_updated,
            stats.nodes_merged,
            stats.edges_upserted,
        )
        return stats

    # -- nodes -------------------------------------------------------------
    def _resolve_node(
        self, node: ExtractedNode, vec: list[float] | None, stats: ResolveStats
    ) -> uuid.UUID:
        if node.is_sourced:
            return self._upsert_sourced_node(node, vec, stats)
        return self._upsert_derived_node(node, vec, stats)

    def _upsert_sourced_node(
        self, node: ExtractedNode, vec: list[float] | None, stats: ResolveStats
    ) -> uuid.UUID:
        stmt = pg_insert(Node).values(
            node_type=node.node_type.value,
            name=node.name,
            summary=node.summary,
            properties=node.props_dict(),
            embedding=vec,
            source_system=node.source_system,
            external_id=node.external_id,
            confidence=node.confidence,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_nodes_identity",
            set_={
                "name": stmt.excluded.name,
                "summary": stmt.excluded.summary,
                # JSONB merge: new keys win, existing keys retained.
                "properties": Node.properties.op("||")(stmt.excluded.properties),
                # Only overwrite the embedding when a fresh one was supplied.
                "embedding": func.coalesce(stmt.excluded.embedding, Node.embedding),
                "confidence": func.greatest(Node.confidence, stmt.excluded.confidence),
                "updated_at": func.now(),
            },
            # The system column xmax is 0 for a freshly INSERTed row and the current
            # txid for a row that was UPDATEd by the conflict clause — lets us tell
            # "created" from "updated" in a single round-trip.
        ).returning(Node.id, literal_column("(xmax::text::int = 0)").label("inserted"))

        row = self.db.execute(stmt).one()
        if row.inserted:
            stats.nodes_created += 1
        else:
            stats.nodes_updated += 1
        return row.id

    def _upsert_derived_node(
        self, node: ExtractedNode, vec: list[float] | None, stats: ResolveStats
    ) -> uuid.UUID:
        match_id = self._find_duplicate(node.node_type.value, node.name, vec)
        if match_id is not None:
            canonical_id = self._canonical_id(match_id)
            self._enrich_node(canonical_id, node, vec)
            stats.nodes_merged += 1
            return canonical_id

        new_node = Node(
            node_type=node.node_type.value,
            name=node.name,
            summary=node.summary,
            properties=node.props_dict(),
            embedding=vec,
            source_system=None,  # derived entities are cross-source / global
            external_id=None,
            confidence=node.confidence,
        )
        self.db.add(new_node)
        self.db.flush()
        stats.nodes_created += 1
        return new_node.id

    def _find_duplicate(
        self, node_type: str, name: str, vec: list[float] | None
    ) -> uuid.UUID | None:
        """Find an existing DERIVED node of the same type that matches by name
        (trigram) OR by embedding (cosine), preferring the closest semantic hit."""
        # The `%` operator (unlike similarity() >= x) is index-accelerated;
        # set_config(..., true) scopes the threshold to this transaction.
        self.db.execute(
            text("SELECT set_config('pg_trgm.similarity_threshold', :threshold, true)"),
            {"threshold": str(self.NAME_SIM_THRESHOLD)},
        )
        name_sim = func.similarity(Node.name, name)
        clauses = [Node.name.op("%")(name)]
        order_by = name_sim.desc()

        if vec is not None:
            vec_dist = Node.embedding.cosine_distance(vec)
            clauses.append((Node.embedding.is_not(None)) & (vec_dist <= self.VEC_DIST_THRESHOLD))
            order_by = vec_dist.asc()  # closest meaning first

        stmt = (
            select(Node.id)
            .where(
                Node.node_type == node_type,
                Node.external_id.is_(None),  # only dedup against derived nodes
                or_(*clauses),
            )
            .order_by(order_by)
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def _canonical_id(self, node_id: uuid.UUID) -> uuid.UUID:
        """Follow the merge chain to the surviving canonical node.

        Hardened against corrupt data: a visited-set + depth cap guarantee
        termination even if `canonical_node_id` pointers form a cycle.
        """
        seen: set[uuid.UUID] = set()
        current = node_id
        for _ in range(16):  # depth cap — chains are realistically 1 hop
            if current in seen:
                break
            seen.add(current)
            nxt = self.db.execute(
                select(Node.canonical_node_id).where(Node.id == current)
            ).scalar_one_or_none()
            if not nxt:
                break
            current = nxt
        return current

    def _enrich_node(
        self, node_id: uuid.UUID, node: ExtractedNode, vec: list[float] | None
    ) -> None:
        """Fold a new mention's information into the surviving node:
        merge properties, backfill a missing summary/embedding, bump confidence."""
        target = self.db.get(Node, node_id)
        if target is None:
            return
        target.properties = {**target.properties, **node.props_dict()}
        if not target.summary and node.summary:
            target.summary = node.summary
        if target.embedding is None and vec is not None:
            target.embedding = vec
        target.confidence = max(target.confidence, node.confidence)
        self.db.flush()

    # -- provenance --------------------------------------------------------
    def _record_mention(
        self, node_id: uuid.UUID, document_id: uuid.UUID, context: str | None
    ) -> bool:
        stmt = (
            pg_insert(NodeMention)
            .values(
                node_id=node_id,
                document_id=document_id,
                context=(context or "")[:1000] or None,
            )
            .on_conflict_do_nothing(constraint="uq_node_mentions_identity")
            .returning(NodeMention.id)
        )
        return self.db.execute(stmt).scalar_one_or_none() is not None

    # -- edges -------------------------------------------------------------
    def _resolve_edge(
        self,
        edge: ExtractedEdge,
        document_id: uuid.UUID,
        temp_to_id: dict[str, uuid.UUID],
    ) -> bool:
        src = temp_to_id.get(edge.source_temp_id)
        tgt = temp_to_id.get(edge.target_temp_id)
        if src is None or tgt is None:
            logger.warning(
                "Skipping edge %s: dangling endpoint (%s -> %s)",
                edge.relationship.value,
                edge.source_temp_id,
                edge.target_temp_id,
            )
            return False

        stmt = pg_insert(Edge).values(
            source_id=src,
            target_id=tgt,
            relationship=edge.relationship.value,
            properties=edge.props_dict(),
            confidence=edge.confidence,
            evidence_document_id=document_id,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_edges_identity",
            set_={
                "confidence": func.greatest(Edge.confidence, stmt.excluded.confidence),
                "properties": Edge.properties.op("||")(stmt.excluded.properties),
                "evidence_document_id": stmt.excluded.evidence_document_id,
                # Each re-observation of the same fact strengthens the edge.
                "weight": Edge.weight + 1.0,
                "updated_at": func.now(),
            },
        )
        self.db.execute(stmt)
        return True
