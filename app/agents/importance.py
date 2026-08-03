"""Importance — PageRank over the graph, stamped into node properties.

Derived, deterministic metadata: recomputing is idempotent and carries no
judgment, so it is a direct job (not a proposal) and, like the digest, is not
registered with the AgentFactory. The score surfaces automatically wherever
node properties are shown (get_node_details, search results); nothing reorders
existing rankings. Run via

    python -m app.agents.run importance
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Edge, Node

logger = logging.getLogger("company_brain.agents.importance")

_DAMPING = 0.85
_ITERATIONS = 30


def pagerank(
    node_ids: list, edges: list[tuple], damping: float = _DAMPING, iterations: int = _ITERATIONS
) -> dict:
    """Plain power iteration, normalized so the top node scores 1.0.

    # ponytail: pure-Python O(V+E) per pass — fine for tens of thousands of
    # nodes; switch to a sparse-matrix pass if it ever drags.
    """
    n = len(node_ids)
    if n == 0:
        return {}
    rank = dict.fromkeys(node_ids, 1.0 / n)
    out_links: dict = {nid: [] for nid in node_ids}
    for src, tgt in edges:
        if src in out_links and tgt in rank:
            out_links[src].append(tgt)

    for _ in range(iterations):
        dangling = damping * sum(rank[nid] for nid, out in out_links.items() if not out) / n
        base = (1.0 - damping) / n + dangling
        new = dict.fromkeys(node_ids, base)
        for src, targets in out_links.items():
            if targets:
                share = damping * rank[src] / len(targets)
                for tgt in targets:
                    new[tgt] += share
        rank = new

    top = max(rank.values()) or 1.0
    return {nid: value / top for nid, value in rank.items()}


def compute_importance(db: Session) -> int:
    """Score every canonical node and stamp `importance` into its properties."""
    nodes = db.scalars(select(Node).where(Node.canonical_node_id.is_(None))).all()
    edges = db.execute(select(Edge.source_id, Edge.target_id)).all()
    scores = pagerank([n.id for n in nodes], [(s, t) for s, t in edges])
    for node in nodes:
        node.properties = {**node.properties, "importance": round(scores[node.id], 4)}
    db.commit()
    logger.info("importance stamped on %d nodes", len(nodes))
    return len(nodes)
