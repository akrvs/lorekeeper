"""Visibility predicates — turn a Principal into row filters.

A node is visible iff it is mentioned in at least one raw_documents row whose
(source_system, resource_key) the principal was granted. This single rule covers
sourced *and* derived (inferred) nodes, since the resolver records a mention for
every node it creates.

Two forms are provided:
  * `visible_node_ids()` — a SQLAlchemy scalar subquery for the ORM repository.
  * `visibility_sql()`   — a raw SQL fragment + params for the recursive-CTE
    traversal (which is hand-written SQL).
"""

from sqlalchemy import and_, false, or_, select

from app.db.models import NodeMention, RawDocument
from app.security.principal import Principal


def _doc_clause(principal: Principal):
    """SQLAlchemy boolean clause over RawDocument for the granted scope.
    Returns None for a superuser (no restriction)."""
    if principal.superuser:
        return None
    ors = []
    for grant in principal.grants:
        if grant.resources is None:
            ors.append(RawDocument.source_system == grant.source_system)
        else:
            ors.append(
                and_(
                    RawDocument.source_system == grant.source_system,
                    RawDocument.resource_key.in_(grant.resources),
                )
            )
    return or_(*ors) if ors else false()  # no grants -> see nothing


def visible_node_ids(principal: Principal):
    """Scalar subquery of node ids the principal may see, or None if unrestricted."""
    clause = _doc_clause(principal)
    if clause is None:
        return None
    return (
        select(NodeMention.node_id)
        .join(RawDocument, RawDocument.id == NodeMention.document_id)
        .where(clause)
    )


def visibility_sql(principal: Principal, node_col: str) -> tuple[str, dict]:
    """A raw SQL boolean fragment asserting `node_col` is visible, plus bind
    params. Used inside the traversal CTE. Superuser -> ('TRUE', {})."""
    if principal.superuser:
        return "TRUE", {}

    wild = [g.source_system for g in principal.grants if g.resources is None]
    pair_src: list[str] = []
    pair_res: list[str] = []
    for g in principal.grants:
        if g.resources is not None:
            for r in g.resources:
                pair_src.append(g.source_system)
                pair_res.append(r)

    if not wild and not pair_src:
        return "FALSE", {}

    conds = []
    params: dict = {}
    if wild:
        conds.append("rd.source_system = ANY(:vis_wild)")
        params["vis_wild"] = wild
    if pair_src:
        conds.append(
            "(rd.source_system, rd.resource_key) IN "
            "(SELECT s, r FROM unnest(:vis_src::text[], :vis_res::text[]) AS t(s, r))"
        )
        params["vis_src"] = pair_src
        params["vis_res"] = pair_res

    fragment = (
        f"EXISTS (SELECT 1 FROM node_mentions nm "
        f"JOIN raw_documents rd ON rd.id = nm.document_id "
        f"WHERE nm.node_id = {node_col} AND ({' OR '.join(conds)}))"
    )
    return fragment, params
