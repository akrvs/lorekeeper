"""GraphRepository — every graph read, visibility-filtered and bounded.

The MCP tools call this; they never query the ORM directly. Visibility (Track 1)
is injected into each query from the Principal; the multi-hop traversal (Track 3)
is a recursive CTE bounded by depth, a server-side statement_timeout, native
CYCLE detection, and a result cap.
"""

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, aliased

from app.config import settings
from app.db.models import Edge, Node, NodeMention, RawDocument
from app.security.principal import Principal
from app.security.visibility import _doc_clause, visibility_sql, visible_node_ids


class GraphRepository:
    def __init__(self, db: Session, principal: Principal):
        self.db = db
        self.principal = principal
        self._scope = visible_node_ids(principal)  # scalar subquery or None (unrestricted)

    # -- helpers -----------------------------------------------------------
    def _restrict(self, stmt, node_col=Node.id):
        return stmt if self._scope is None else stmt.where(node_col.in_(self._scope))

    def is_visible(self, node_id: uuid.UUID) -> bool:
        if self._scope is None:
            return True
        return (
            self.db.scalar(select(Node.id).where(Node.id == node_id, Node.id.in_(self._scope)))
            is not None
        )

    # -- reads -------------------------------------------------------------
    def semantic_search(self, qvec, node_type: str | None, limit: int):
        distance = Node.embedding.cosine_distance(qvec).label("distance")
        # Merged duplicates (canonical_node_id set) stay in the table as aliases
        # for the resolver, but must not surface as separate search results.
        stmt = select(Node, distance).where(
            Node.embedding.is_not(None), Node.canonical_node_id.is_(None)
        )
        if node_type:
            stmt = stmt.where(Node.node_type == node_type)
        stmt = self._restrict(stmt).order_by(distance).limit(limit)
        return self.db.execute(stmt).all()

    def get_node(self, node_id: uuid.UUID) -> Node | None:
        node = self.db.get(Node, node_id)
        if node is None or not self.is_visible(node_id):
            return None
        return node

    def node_degree(self, node_id: uuid.UUID) -> tuple[int, int]:
        out_deg = self.db.scalar(
            select(func.count()).select_from(Edge).where(Edge.source_id == node_id)
        )
        in_deg = self.db.scalar(
            select(func.count()).select_from(Edge).where(Edge.target_id == node_id)
        )
        return out_deg, in_deg

    def node_sources(self, node_id: uuid.UUID):
        """Source documents behind a node — only those the principal may see."""
        clause = _doc_clause(self.principal)
        stmt = (
            select(
                RawDocument.source_system,
                RawDocument.source_type,
                RawDocument.title,
                RawDocument.url,
            )
            .join(NodeMention, NodeMention.document_id == RawDocument.id)
            .where(NodeMention.node_id == node_id)
        )
        if clause is not None:
            stmt = stmt.where(clause)
        return self.db.execute(stmt.order_by(RawDocument.source_system)).all()

    def stats(self):
        node_stmt = self._restrict(
            select(Node.node_type, func.count()).where(Node.canonical_node_id.is_(None))
        )
        node_counts = self.db.execute(
            node_stmt.group_by(Node.node_type).order_by(func.count().desc())
        ).all()
        edge_stmt = select(Edge.relationship_type, func.count())
        if self._scope is not None:
            edge_stmt = edge_stmt.where(
                Edge.source_id.in_(self._scope), Edge.target_id.in_(self._scope)
            )
        edge_counts = self.db.execute(
            edge_stmt.group_by(Edge.relationship_type).order_by(func.count().desc())
        ).all()
        stale_stmt = self._restrict(
            select(func.count())
            .select_from(Node)
            .where(
                Node.canonical_node_id.is_(None),
                func.coalesce(Node.properties["stale"].astext, "false") == "true",
            )
        )
        stale = self.db.scalar(stale_stmt)
        return node_counts, edge_counts, stale

    def stale_nodes(self, limit: int):
        """Stale-flagged canonical nodes, oldest evidence first."""
        stmt = self._restrict(
            select(Node).where(
                Node.canonical_node_id.is_(None),
                func.coalesce(Node.properties["stale"].astext, "false") == "true",
            )
        )
        stmt = stmt.order_by(
            Node.properties["stale_since"].astext.asc().nulls_last(), Node.name
        ).limit(limit)
        return self.db.scalars(stmt).all()

    def recent_nodes(self, since, limit: int):
        """Canonical nodes created since a cutoff, newest first."""
        stmt = self._restrict(
            select(Node).where(Node.canonical_node_id.is_(None), Node.created_at >= since)
        )
        return self.db.scalars(stmt.order_by(Node.created_at.desc()).limit(limit)).all()

    def node_timeline(self, node_id: uuid.UUID, limit: int):
        clause = _doc_clause(self.principal)
        ts = func.coalesce(
            RawDocument.source_updated_at, RawDocument.source_created_at, RawDocument.ingested_at
        ).label("ts")
        stmt = (
            select(
                ts,
                RawDocument.source_system,
                RawDocument.source_type,
                RawDocument.title,
                RawDocument.author,
                RawDocument.url,
            )
            .join(NodeMention, NodeMention.document_id == RawDocument.id)
            .where(NodeMention.node_id == node_id)
        )
        if clause is not None:
            stmt = stmt.where(clause)
        return self.db.execute(stmt.order_by(ts.desc().nulls_last()).limit(limit)).all()

    def neighbors(self, node_id: uuid.UUID, direction: str, limit: int):
        result: dict[str, list] = {"outgoing": [], "incoming": []}
        if direction in ("outgoing", "both"):
            tgt = aliased(Node)
            stmt = (
                select(
                    Edge.relationship_type,
                    Edge.confidence,
                    Edge.weight,
                    tgt.id,
                    tgt.name,
                    tgt.node_type,
                )
                .join(tgt, tgt.id == Edge.target_id)
                .where(Edge.source_id == node_id)
            )
            stmt = self._restrict(stmt, tgt.id).order_by(Edge.relationship_type).limit(limit)
            result["outgoing"] = self.db.execute(stmt).all()
        if direction in ("incoming", "both"):
            src = aliased(Node)
            stmt = (
                select(
                    Edge.relationship_type,
                    Edge.confidence,
                    Edge.weight,
                    src.id,
                    src.name,
                    src.node_type,
                )
                .join(src, src.id == Edge.source_id)
                .where(Edge.target_id == node_id)
            )
            stmt = self._restrict(stmt, src.id).order_by(Edge.relationship_type).limit(limit)
            result["incoming"] = self.db.execute(stmt).all()
        return result

    def traverse(self, start: Node, target_node_type: str, max_depth: int, limit: int):
        """Bounded, visibility-aware multi-hop walk (edges followed both ways)."""
        max_depth = max(1, min(int(max_depth), settings.traverse_max_depth))
        vis_fragment, vis_params = visibility_sql(self.principal, "nn.id")

        # Server-side execution boundary: a runaway walk on a dense graph is
        # killed by the DB rather than hanging the request. SET LOCAL is scoped
        # to this transaction. The int() guards against injection.
        self.db.execute(
            text(f"SET LOCAL statement_timeout = {int(settings.traverse_statement_timeout_ms)}")
        )

        sql = text(
            f"""
WITH RECURSIVE walk AS (
  SELECT CAST(:start_id AS uuid) AS node_id,
         ARRAY[CAST(:start_name AS text)] AS name_path,
         ARRAY[]::text[] AS rel_path,
         0 AS depth
  UNION ALL
  SELECT step.next_id,
         walk.name_path || nn.name,
         walk.rel_path || (step.dir || step.rel),
         walk.depth + 1
  FROM walk
  JOIN LATERAL (
    SELECT
      CASE WHEN e.source_id = walk.node_id THEN e.target_id ELSE e.source_id END AS next_id,
      CASE WHEN e.source_id = walk.node_id THEN '>' ELSE '<' END AS dir,
      e.relationship AS rel
    FROM edges e
    WHERE e.source_id = walk.node_id OR e.target_id = walk.node_id
  ) AS step ON TRUE
  JOIN nodes nn ON nn.id = step.next_id
  WHERE walk.depth < :max_depth AND {vis_fragment}
) CYCLE node_id SET is_cycle USING cyclepath
SELECT DISTINCT ON (n.id) n.id, n.name, n.node_type, walk.depth, walk.name_path, walk.rel_path
FROM walk
JOIN nodes n ON n.id = walk.node_id
WHERE walk.depth > 0 AND NOT walk.is_cycle AND n.node_type = :target_type
ORDER BY n.id, walk.depth
"""
        )
        params = {
            "start_id": str(start.id),
            "start_name": start.name,
            "max_depth": max_depth,
            "target_type": target_node_type,
            **vis_params,
        }
        rows = self.db.execute(sql, params).all()
        return sorted(rows, key=lambda r: r.depth)[:limit]

    def find_path(self, start: Node, target_id: uuid.UUID, max_depth: int):
        """Shortest visible path from start to one specific node (edges both ways)."""
        max_depth = max(1, min(int(max_depth), settings.traverse_max_depth))
        vis_fragment, vis_params = visibility_sql(self.principal, "nn.id")

        self.db.execute(
            text(f"SET LOCAL statement_timeout = {int(settings.traverse_statement_timeout_ms)}")
        )

        sql = text(
            f"""
WITH RECURSIVE walk AS (
  SELECT CAST(:start_id AS uuid) AS node_id,
         ARRAY[CAST(:start_name AS text)] AS name_path,
         ARRAY[]::text[] AS rel_path,
         0 AS depth
  UNION ALL
  SELECT step.next_id,
         walk.name_path || nn.name,
         walk.rel_path || (step.dir || step.rel),
         walk.depth + 1
  FROM walk
  JOIN LATERAL (
    SELECT
      CASE WHEN e.source_id = walk.node_id THEN e.target_id ELSE e.source_id END AS next_id,
      CASE WHEN e.source_id = walk.node_id THEN '>' ELSE '<' END AS dir,
      e.relationship AS rel
    FROM edges e
    WHERE e.source_id = walk.node_id OR e.target_id = walk.node_id
  ) AS step ON TRUE
  JOIN nodes nn ON nn.id = step.next_id
  WHERE walk.depth < :max_depth AND {vis_fragment}
) CYCLE node_id SET is_cycle USING cyclepath
SELECT walk.depth, walk.name_path, walk.rel_path
FROM walk
WHERE walk.depth > 0 AND NOT walk.is_cycle AND walk.node_id = :target_id
ORDER BY walk.depth
LIMIT 1
"""
        )
        params = {
            "start_id": str(start.id),
            "start_name": start.name,
            "max_depth": max_depth,
            "target_id": str(target_id),
            **vis_params,
        }
        return self.db.execute(sql, params).first()
