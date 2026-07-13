"""Company Brain MCP server.

Exposes the knowledge graph to coding agents (Cursor, Claude Desktop, Windsurf,
Gemini) over the Model Context Protocol using the official `mcp` SDK's FastMCP.
Default transport is stdio — the transport Cursor/Claude Desktop launch.

Run:  python -m app.mcp.server

Every tool:
  * accepts an optional `principal_token` (Track 1). With RBAC disabled it's an
    unrestricted superuser; with RBAC enabled, results are row-filtered to the
    caller's granted data sources, and the call is audited.
  * reads exclusively through GraphRepository (the single visibility choke point).
  * `semantic_search` / `get_node_neighbors` are TTL-cached, keyed by the
    principal's visibility scope (Track 3).

IMPORTANT (stdio): the JSON-RPC protocol owns stdout. All logging goes to stderr
and tools must never print() — they return strings.
"""

import json
import logging
import sys
import uuid

from app.cache import get_cache, make_key
from app.config import settings
from app.db.init_db import wait_for_db
from app.db.session import SessionLocal, engine
from app.llm import get_llm_provider
from app.repositories import GraphRepository
from app.security import AuditLogger, get_identity_resolver

# stderr only — stdout is reserved for the MCP wire protocol.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("company_brain.mcp")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("The 'mcp' package is required: pip install 'mcp>=1.2,<2'") from exc


mcp = FastMCP(
    "company-brain",
    instructions=(
        "Company Brain is an organizational knowledge graph built from GitHub, Slack and "
        "other sources. Typical flow: (1) semantic_search to find the entity you care about "
        "and get its node_id; (2) get_node_details to read its attributes and the source "
        "documents/URLs behind it; (3) get_node_neighbors to see what it directly connects "
        "to; (4) traverse_graph_path to find multi-hop connections between two kinds of "
        "things. Pass principal_token to scope results to a user's permissions. "
        "Node ids are UUIDs returned by these tools."
    ),
)

_NS = "graph"  # cache namespace bumped by ingestion


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _props(properties: dict | None) -> str:
    return json.dumps(properties, ensure_ascii=False, sort_keys=True) if properties else "{}"


def _node_line(name: str, node_type: str, node_id: uuid.UUID) -> str:
    return f"{name}  ({node_type})  id={node_id}"


def _parse_uuid(node_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(node_id))
    except (ValueError, TypeError):
        return None


def _principal(db, token):
    return get_identity_resolver().resolve(db, token)


def _render_path(name_path: list[str], rel_path: list[str]) -> str:
    """Turn parallel name/relationship arrays into 'A -[REL]-> B <-[REL2]- C'."""
    parts: list[str] = []
    for i, rel in enumerate(rel_path):
        direction, label = rel[0], rel[1:]
        arrow = f"-[{label}]->" if direction == ">" else f"<-[{label}]-"
        parts.append(f"{name_path[i]} {arrow} ")
    parts.append(name_path[-1])
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def semantic_search(
    query: str, node_type: str | None = None, limit: int = 5, principal_token: str | None = None
) -> str:
    """Find entities semantically related to a free-text query (pgvector cosine).

    Args:
        query: Natural-language description of what you're looking for.
        node_type: Optional filter, e.g. 'feature', 'incident', 'repository'.
        limit: Max results (1-50, default 5).
        principal_token: Optional identity token to scope results (RBAC).

    Returns a ranked list of entities with similarity score, node_type, UUID,
    summary and properties.
    """
    limit = max(1, min(int(limit), 50))
    with SessionLocal() as db:
        try:
            principal = _principal(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."

        cache = get_cache()
        key = make_key(
            _NS,
            cache.version(_NS),
            "search",
            principal.scope_key(),
            query,
            str(node_type),
            str(limit),
        )
        cached = cache.get(key)
        if cached is not None:
            return cached

        try:
            qvec = get_llm_provider().embed([query])[0]
        except Exception as exc:  # noqa: BLE001
            return (
                "ERROR: embedding provider unavailable "
                f"({exc}). Configure AZURE_OPENAI_* or set LLM_PROVIDER=stub."
            )

        rows = GraphRepository(db, principal).semantic_search(qvec, node_type, limit)
        AuditLogger(db).record(
            principal,
            "semantic_search",
            {"query": query, "node_type": node_type},
            [n.id for n, _ in rows],
        )

        if not rows:
            return f'No visible entities found for query "{query}".'
        out = [
            f'Top {len(rows)} matches for "{query}"'
            + (f" (node_type={node_type})" if node_type else "")
            + ":\n"
        ]
        for i, (node, dist) in enumerate(rows, 1):
            out.append(
                f"{i}. {_node_line(node.name, node.node_type, node.id)}\n"
                f"   similarity: {1.0 - float(dist):.3f}\n"
                f"   summary: {node.summary or '(none)'}\n"
                f"   properties: {_props(node.properties)}"
            )
        result = "\n".join(out)
        cache.set(key, result, settings.cache_ttl_seconds)
        return result


@mcp.tool()
def get_node_details(node_id: str, principal_token: str | None = None) -> str:
    """Return everything known about one entity, including provenance.

    Args:
        node_id: UUID of the node (from semantic_search or another tool).
        principal_token: Optional identity token to scope access (RBAC).

    Returns attributes, JSONB properties, edge degree, and the raw source
    documents (with URLs) the node was mentioned in.
    """
    nid = _parse_uuid(node_id)
    if nid is None:
        return f"ERROR: '{node_id}' is not a valid UUID."

    with SessionLocal() as db:
        try:
            principal = _principal(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."
        repo = GraphRepository(db, principal)
        node = repo.get_node(nid)
        if node is None:
            return f"No node found with id {node_id} (or not authorized)."
        out_deg, in_deg = repo.node_degree(nid)
        mentions = repo.node_sources(nid)
        AuditLogger(db).record(principal, "get_node_details", {"node_id": node_id}, [nid])

    lines = [
        f"NODE {_node_line(node.name, node.node_type, node.id)}",
        f"  summary: {node.summary or '(none)'}",
        f"  source: {node.source_system or 'derived'}"
        + (
            f" / external_id={node.external_id}"
            if node.external_id
            else " (inferred, no external id)"
        ),
        f"  confidence: {node.confidence:.2f}",
        f"  degree: {out_deg} outgoing, {in_deg} incoming",
        f"  properties: {_props(node.properties)}",
        "",
        f"SOURCE DOCUMENTS ({len(mentions)}):",
    ]
    if not mentions:
        lines.append("  (none visible)")
    for src, stype, title, url in mentions:
        lines.append(f"  - [{src}/{stype}] {title or '(untitled)'}  {url or '(no url)'}")
    return "\n".join(lines)


@mcp.tool()
def get_node_neighbors(
    node_id: str, direction: str = "both", limit: int = 50, principal_token: str | None = None
) -> str:
    """List the entities directly connected to a node (1 hop).

    Args:
        node_id: UUID of the node.
        direction: 'outgoing', 'incoming', or 'both' (default).
        limit: Max edges per direction (default 50).
        principal_token: Optional identity token to scope results (RBAC).
    """
    nid = _parse_uuid(node_id)
    if nid is None:
        return f"ERROR: '{node_id}' is not a valid UUID."
    direction = direction.lower()
    if direction not in ("outgoing", "incoming", "both"):
        return "ERROR: direction must be 'outgoing', 'incoming', or 'both'."
    limit = max(1, min(int(limit), 200))

    with SessionLocal() as db:
        try:
            principal = _principal(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."

        cache = get_cache()
        key = make_key(
            _NS,
            cache.version(_NS),
            "neighbors",
            principal.scope_key(),
            node_id,
            direction,
            str(limit),
        )
        cached = cache.get(key)
        if cached is not None:
            return cached

        repo = GraphRepository(db, principal)
        node = repo.get_node(nid)
        if node is None:
            return f"No node found with id {node_id} (or not authorized)."
        neighbors = repo.neighbors(nid, direction, limit)
        AuditLogger(db).record(principal, "get_node_neighbors", {"node_id": node_id}, [nid])

        lines = [f"NEIGHBORS of {_node_line(node.name, node.node_type, node.id)}", ""]
        if direction in ("outgoing", "both"):
            rows = neighbors["outgoing"]
            lines.append(f"OUTGOING ({len(rows)}):")
            for rel, conf, weight, tid, tname, ttype in rows:
                lines.append(
                    f"  -[{rel}]->  {_node_line(tname, ttype, tid)}  "
                    f"(conf={conf:.2f}, weight={weight:.1f})"
                )
            lines.append("  (none)" if not rows else "")
        if direction in ("incoming", "both"):
            rows = neighbors["incoming"]
            lines.append(f"INCOMING ({len(rows)}):")
            for rel, conf, weight, sid, sname, stype in rows:
                lines.append(
                    f"  <-[{rel}]-  {_node_line(sname, stype, sid)}  "
                    f"(conf={conf:.2f}, weight={weight:.1f})"
                )
            if not rows:
                lines.append("  (none)")

        result = "\n".join(lines)
        cache.set(key, result, settings.cache_ttl_seconds)
        return result


@mcp.tool()
def traverse_graph_path(
    start_node_id: str,
    target_node_type: str,
    max_depth: int = 3,
    limit: int = 10,
    principal_token: str | None = None,
) -> str:
    """Find multi-hop paths from a starting node to entities of a target type.

    Uncovers indirect connections between disparate concepts — e.g. from a
    'repository' to the 'slack_thread's about an incident it caused. Edges are
    traversed in both directions; the walk is bounded by depth, a server-side
    statement timeout, and a result cap.

    Args:
        start_node_id: UUID of the node to start from.
        target_node_type: Node type to reach, e.g. 'slack_thread', 'feature'.
        max_depth: Max hops (default 3).
        limit: Max distinct target nodes to report (default 10).
        principal_token: Optional identity token to scope traversal (RBAC).
    """
    sid = _parse_uuid(start_node_id)
    if sid is None:
        return f"ERROR: '{start_node_id}' is not a valid UUID."
    limit = max(1, min(int(limit), 50))

    with SessionLocal() as db:
        try:
            principal = _principal(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."
        repo = GraphRepository(db, principal)
        start = repo.get_node(sid)
        if start is None:
            return f"No node found with id {start_node_id} (or not authorized)."
        rows = repo.traverse(start, target_node_type, max_depth, limit)
        AuditLogger(
            db,
        ).record(
            principal,
            "traverse_graph_path",
            {"start": start_node_id, "target": target_node_type},
            [r.id for r in rows],
        )

    if not rows:
        return (
            f"No '{target_node_type}' nodes reachable from "
            f"{_node_line(start.name, start.node_type, start.id)} within {max_depth} hops."
        )
    out = [
        f"Paths from {_node_line(start.name, start.node_type, start.id)} "
        f"to '{target_node_type}' nodes:\n"
    ]
    for tid, tname, ttype, depth, name_path, rel_path in rows:
        out.append(f"- {tname}  ({ttype})  id={tid}  [{depth} hop(s)]")
        out.append("    " + _render_path(name_path, rel_path))
    return "\n".join(out)


def main() -> None:
    logger.info("Company Brain MCP server starting (stdio transport)...")
    wait_for_db(engine)  # tolerate the DB still warming up
    mcp.run()  # defaults to stdio


if __name__ == "__main__":
    main()
