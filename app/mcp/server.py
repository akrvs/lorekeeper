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
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.cache import get_cache, make_key
from app.config import settings
from app.db.init_db import wait_for_db
from app.db.models.proposal import APPLIED_STATUSES, Proposal
from app.db.models.source import IngestionRun
from app.db.session import SessionLocal, engine
from app.llm import get_llm_provider
from app.proposals import ProposalEngine, ProposalError
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
        "Node ids are UUIDs returned by these tools. The graph maintains itself: "
        "agents file change proposals (duplicate merges, ...) that you can inspect "
        "with review_proposals and decide with approve_proposal / reject_proposal; "
        "rollback_proposal undoes an applied change."
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
def get_entity_timeline(node_id: str, limit: int = 20, principal_token: str | None = None) -> str:
    """Chronological activity feed for an entity, newest first.

    Lists the dated source documents (PRs, issues, threads, pages) that mention
    the entity, so you can see what happened around it and when.

    Args:
        node_id: UUID of the node (from semantic_search or another tool).
        limit: Max events to return (1-100, default 20).
        principal_token: Optional identity token to scope results (RBAC).
    """
    nid = _parse_uuid(node_id)
    if nid is None:
        return f"ERROR: '{node_id}' is not a valid UUID."
    limit = max(1, min(int(limit), 100))

    with SessionLocal() as db:
        try:
            principal = _principal(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."
        repo = GraphRepository(db, principal)
        node = repo.get_node(nid)
        if node is None:
            return f"No node found with id {node_id} (or not authorized)."
        rows = repo.node_timeline(nid, limit)
        AuditLogger(db).record(principal, "get_entity_timeline", {"node_id": node_id}, [nid])

    if not rows:
        return f"No activity found for {_node_line(node.name, node.node_type, node.id)}."
    lines = [
        f"TIMELINE for {_node_line(node.name, node.node_type, node.id)} "
        f"({len(rows)} event(s), newest first):"
    ]
    for ts, src, stype, title, author, url in rows:
        when = ts.date().isoformat() if ts else "(undated)"
        lines.append(
            f"  {when}  [{src}/{stype}] {title or '(untitled)'}"
            f"  by {author or 'unknown'}  {url or '(no url)'}"
        )
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
        AuditLogger(db).record(
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


@mcp.tool()
def get_graph_stats(principal_token: str | None = None) -> str:
    """Health snapshot of the knowledge graph.

    Reports node counts by type, edge counts by relationship, stale-flagged
    nodes, the proposal queue by status, and the last completed sync per source.

    Args:
        principal_token: Optional identity token to scope results (RBAC).
    """
    with SessionLocal() as db:
        try:
            principal = _principal(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."
        node_counts, edge_counts, stale = GraphRepository(db, principal).stats()
        proposal_counts = db.execute(
            select(Proposal.status, func.count()).group_by(Proposal.status)
        ).all()
        last_runs = db.execute(
            select(IngestionRun.source_system, func.max(IngestionRun.finished_at))
            .where(IngestionRun.status == "completed")
            .group_by(IngestionRun.source_system)
            .order_by(IngestionRun.source_system)
        ).all()
        AuditLogger(db).record(principal, "get_graph_stats", {}, [])

    total_nodes = sum(c for _, c in node_counts)
    total_edges = sum(c for _, c in edge_counts)
    lines = [f"GRAPH STATS — {total_nodes} nodes, {total_edges} edges, {stale} stale-flagged", ""]
    lines.append("NODES BY TYPE:")
    lines += [f"  {t}: {c}" for t, c in node_counts] or ["  (empty)"]
    lines.append("EDGES BY RELATIONSHIP:")
    lines += [f"  {r}: {c}" for r, c in edge_counts] or ["  (empty)"]
    lines.append("PROPOSALS BY STATUS:")
    lines += [f"  {s}: {c}" for s, c in proposal_counts] or ["  (none)"]
    lines.append("LAST COMPLETED SYNC PER SOURCE:")
    lines += [f"  {src}: {ts.isoformat() if ts else '(unknown)'}" for src, ts in last_runs] or [
        "  (no completed runs)"
    ]
    return "\n".join(lines)


@mcp.tool()
def get_stale_nodes(limit: int = 20, principal_token: str | None = None) -> str:
    """List stale-flagged entities, oldest evidence first.

    Nodes flagged by the staleness agent carry stale=true and stale_since in
    their properties. Facts listed here keep their citations but their newest
    evidence has gone quiet — treat them with caution or refresh the source.

    Args:
        limit: Max nodes to return (1-100, default 20).
        principal_token: Optional identity token to scope results (RBAC).
    """
    limit = max(1, min(int(limit), 100))
    with SessionLocal() as db:
        try:
            principal = _principal(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."
        rows = GraphRepository(db, principal).stale_nodes(limit)
        AuditLogger(db).record(principal, "get_stale_nodes", {"limit": limit}, [n.id for n in rows])

    if not rows:
        return "No stale-flagged nodes — everything in the graph has fresh evidence."
    lines = [f"{len(rows)} stale-flagged node(s), oldest evidence first:"]
    for node in rows:
        since = node.properties.get("stale_since") or "(unknown)"
        lines.append(f"- {_node_line(node.name, node.node_type, node.id)}  stale_since={since}")
    return "\n".join(lines)


@mcp.tool()
def get_recent_changes(days: int = 7, limit: int = 25, principal_token: str | None = None) -> str:
    """What changed in the graph recently: new entities and applied maintenance.

    Answers "what's new since last week": entities created in the window and
    the self-maintenance proposals (merges, stale flags, schema additions)
    that were applied in it.

    Args:
        days: Window size in days (1-365, default 7).
        limit: Max items per section (1-100, default 25).
        principal_token: Optional identity token to scope results (RBAC).
    """
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    since = datetime.now(UTC) - timedelta(days=days)

    with SessionLocal() as db:
        try:
            principal = _principal(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."
        nodes = GraphRepository(db, principal).recent_nodes(since, limit)
        proposals = db.scalars(
            select(Proposal)
            .where(Proposal.status.in_(APPLIED_STATUSES), Proposal.applied_at >= since)
            .order_by(Proposal.applied_at.desc())
            .limit(limit)
        ).all()
        AuditLogger(db).record(
            principal, "get_recent_changes", {"days": days}, [n.id for n in nodes]
        )

        lines = [f"GRAPH CHANGES in the last {days} day(s):", ""]
        lines.append(f"NEW ENTITIES ({len(nodes)}):")
        if not nodes:
            lines.append("  (none)")
        for node in nodes:
            lines.append(
                f"  {node.created_at.date().isoformat()}  "
                f"{_node_line(node.name, node.node_type, node.id)}"
            )
        lines.append("")
        lines.append(f"APPLIED MAINTENANCE ({len(proposals)}):")
        if not proposals:
            lines.append("  (none)")
        for p in proposals:
            lines.append(f"  {p.applied_at.date().isoformat()}  {_proposal_line(p)}")
        return "\n".join(lines)


@mcp.tool()
def ask(question: str, limit: int = 3, principal_token: str | None = None) -> str:
    """Answer a question from the graph in one call: search, connect, cite.

    Combines semantic_search, get_node_neighbors and source provenance into a
    single response: the entities most relevant to the question, what each one
    connects to, and the source documents (with URLs) behind each claim. Use
    the returned node ids with the other tools to dig deeper.

    Args:
        question: Natural-language question about the organization.
        limit: Max entities to report (1-10, default 3).
        principal_token: Optional identity token to scope results (RBAC).
    """
    limit = max(1, min(int(limit), 10))
    with SessionLocal() as db:
        try:
            principal = _principal(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."
        try:
            qvec = get_llm_provider().embed([question])[0]
        except Exception as exc:  # noqa: BLE001
            return (
                "ERROR: embedding provider unavailable "
                f"({exc}). Configure AZURE_OPENAI_* or set LLM_PROVIDER=stub."
            )
        repo = GraphRepository(db, principal)
        hits = repo.semantic_search(qvec, None, limit)
        AuditLogger(db).record(principal, "ask", {"question": question}, [n.id for n, _ in hits])
        if not hits:
            return f'No visible entities found for "{question}".'

        out = [f'ANSWER CONTEXT for "{question}" — {len(hits)} entities, best match first:']
        for node, dist in hits:
            out.append("")
            out.append(f"ENTITY {_node_line(node.name, node.node_type, node.id)}")
            out.append(f"  similarity: {1.0 - float(dist):.3f}")
            out.append(f"  summary: {node.summary or '(none)'}")
            neighbors = repo.neighbors(node.id, "both", 5)
            for rel, _conf, _weight, tid, tname, ttype in neighbors["outgoing"]:
                out.append(f"  -[{rel}]->  {_node_line(tname, ttype, tid)}")
            for rel, _conf, _weight, sid, sname, stype in neighbors["incoming"]:
                out.append(f"  <-[{rel}]-  {_node_line(sname, stype, sid)}")
            sources = repo.node_sources(node.id)
            out.append(f"  SOURCES ({len(sources)}):")
            if not sources:
                out.append("    (none visible)")
            for src, stype, title, url in sources:
                out.append(f"    - [{src}/{stype}] {title or '(untitled)'}  {url or '(no url)'}")
        return "\n".join(out)


def _mermaid_label(name: str, node_type: str) -> str:
    clean = name.replace('"', "'").replace("[", "(").replace("]", ")")
    return f'"{clean} ({node_type})"'


@mcp.tool()
def export_subgraph(
    node_id: str, depth: int = 1, max_edges: int = 50, principal_token: str | None = None
) -> str:
    """Export an entity's neighborhood as a Mermaid diagram for human viewing.

    Returns 'graph LR' Mermaid source. Paste it into any Mermaid renderer
    (GitHub markdown, mermaid.live, Notion) to see the subgraph.

    Args:
        node_id: UUID of the node at the center of the subgraph.
        depth: How many hops out to expand (1-3, default 1).
        max_edges: Cap on edges in the diagram (1-200, default 50).
        principal_token: Optional identity token to scope results (RBAC).
    """
    nid = _parse_uuid(node_id)
    if nid is None:
        return f"ERROR: '{node_id}' is not a valid UUID."
    depth = max(1, min(int(depth), 3))
    max_edges = max(1, min(int(max_edges), 200))

    with SessionLocal() as db:
        try:
            principal = _principal(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."
        repo = GraphRepository(db, principal)
        start = repo.get_node(nid)
        if start is None:
            return f"No node found with id {node_id} (or not authorized)."

        labels: dict[uuid.UUID, str] = {start.id: _mermaid_label(start.name, start.node_type)}
        edges: list[tuple[uuid.UUID, str, uuid.UUID]] = []
        seen: set[tuple[uuid.UUID, str, uuid.UUID]] = set()
        frontier = [start.id]
        for _ in range(depth):
            next_frontier: list[uuid.UUID] = []
            for current in frontier:
                if len(edges) >= max_edges:
                    break
                neighbors = repo.neighbors(current, "both", max_edges)
                for rel, _conf, _weight, tid, tname, ttype in neighbors["outgoing"]:
                    key = (current, rel, tid)
                    if key in seen or len(edges) >= max_edges:
                        continue
                    seen.add(key)
                    edges.append(key)
                    if tid not in labels:
                        labels[tid] = _mermaid_label(tname, ttype)
                        next_frontier.append(tid)
                for rel, _conf, _weight, sid, sname, stype in neighbors["incoming"]:
                    key = (sid, rel, current)
                    if key in seen or len(edges) >= max_edges:
                        continue
                    seen.add(key)
                    edges.append(key)
                    if sid not in labels:
                        labels[sid] = _mermaid_label(sname, stype)
                        next_frontier.append(sid)
            frontier = next_frontier
        AuditLogger(db).record(
            principal, "export_subgraph", {"node_id": node_id, "depth": depth}, list(labels)
        )

    if not edges:
        return f"{_node_line(start.name, start.node_type, start.id)} has no visible edges."
    short = {node: f"n{i}" for i, node in enumerate(labels)}
    lines = ["graph LR"]
    lines += [f"  {short[node]}[{label}]" for node, label in labels.items()]
    lines += [f"  {short[src]} -->|{rel}| {short[tgt]}" for src, rel, tgt in edges]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Self-maintenance: the proposal review queue
# --------------------------------------------------------------------------- #
def _reviewer(db, token):
    """Resolve the principal and gate write access to the proposal queue.
    With RBAC off this is the anonymous superuser (open-source default)."""
    principal = _principal(db, token)
    if settings.rbac_enabled and not principal.superuser:
        raise PermissionError("proposal review requires a superuser principal")
    return principal


def _proposal_line(p) -> str:
    detail = json.dumps(p.payload, ensure_ascii=False)
    if p.kind == "entity_merge" and p.evidence:
        loser = p.evidence.get("loser", {}).get("name", "?")
        winner = p.evidence.get("winner", {}).get("name", "?")
        detail = f"merge '{loser}' -> '{winner}'"
    elif p.kind in ("schema_node_type", "schema_relationship_type"):
        detail = f"add '{p.payload.get('name')}' to the ontology: {p.payload.get('description')}"
    elif p.kind == "stale_flag":
        detail = p.payload.get("reason", detail)
    return (
        f"- id={p.id}  [{p.kind}]  confidence={p.confidence:.2f}  "
        f"agent={p.agent}  status={p.status}\n    {detail}"
    )


@mcp.tool()
def review_proposals(
    status: str = "pending", limit: int = 20, principal_token: str | None = None
) -> str:
    """List self-maintenance proposals filed by the graph's agents.

    Lorekeeper's maintenance agents (dedup, drift, staleness) never edit the
    graph directly — they file proposals here. Inspect the queue, then use
    approve_proposal / reject_proposal to decide, or rollback_proposal to undo.

    Args:
        status: 'pending' (default), 'applied', 'auto_applied', 'rejected',
            'rolled_back', 'failed', or 'all'.
        limit: Max proposals to list (default 20).
        principal_token: Optional identity token (RBAC: superuser only).
    """
    limit = max(1, min(int(limit), 100))
    with SessionLocal() as db:
        try:
            principal = _reviewer(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."
        stmt = select(Proposal).order_by(Proposal.confidence.desc(), Proposal.created_at)
        if status != "all":
            stmt = stmt.where(Proposal.status == status)
        rows = db.scalars(stmt.limit(limit)).all()
        AuditLogger(db).record(principal, "review_proposals", {"status": status}, [])
        if not rows:
            return f"The proposal queue is clean — no '{status}' proposals."
        header = f"{len(rows)} '{status}' proposal(s), highest confidence first:\n"
        return header + "\n".join(_proposal_line(p) for p in rows)


def _decide_proposal(action: str, proposal_id: str, principal_token: str | None) -> str:
    pid = _parse_uuid(proposal_id)
    if pid is None:
        return f"ERROR: '{proposal_id}' is not a valid UUID."
    with SessionLocal() as db:
        try:
            principal = _reviewer(db, principal_token)
        except PermissionError as exc:
            return f"ERROR: authorization failed ({exc})."
        engine_ = ProposalEngine(db)
        try:
            proposal = getattr(engine_, action)(pid, reviewed_by=principal.subject)
        except ProposalError as exc:
            return f"ERROR: {exc}"
        AuditLogger(db).record(principal, f"{action}_proposal", {"proposal_id": proposal_id}, [])
        return f"OK: proposal {proposal.id} ({proposal.kind}) is now '{proposal.status}'."


@mcp.tool()
def approve_proposal(proposal_id: str, principal_token: str | None = None) -> str:
    """Apply a pending self-maintenance proposal to the graph.

    The change is validated against the current graph, applied atomically, and
    a rollback snapshot is stored — rollback_proposal undoes it exactly.

    Args:
        proposal_id: UUID from review_proposals.
        principal_token: Optional identity token (RBAC: superuser only).
    """
    return _decide_proposal("approve", proposal_id, principal_token)


@mcp.tool()
def reject_proposal(proposal_id: str, principal_token: str | None = None) -> str:
    """Reject a pending proposal. Sticky: agents will never re-file the same change.

    Args:
        proposal_id: UUID from review_proposals.
        principal_token: Optional identity token (RBAC: superuser only).
    """
    return _decide_proposal("reject", proposal_id, principal_token)


@mcp.tool()
def rollback_proposal(proposal_id: str, principal_token: str | None = None) -> str:
    """Undo an applied/auto-applied proposal from its snapshot, restoring the prior graph.

    Args:
        proposal_id: UUID of an applied proposal.
        principal_token: Optional identity token (RBAC: superuser only).
    """
    return _decide_proposal("rollback", proposal_id, principal_token)


def main() -> None:
    logger.info("Company Brain MCP server starting (stdio transport)...")
    wait_for_db(engine)  # tolerate the DB still warming up
    mcp.run()  # defaults to stdio


if __name__ == "__main__":
    main()
