"""Graph export/import — full-fidelity backup, plus a human-facing HTML viewer.

    python -m app.export dump graph.jsonl
    python -m app.export load graph.jsonl
    python -m app.export html graph.html [--node ID --depth N]

Dump/load: one JSON line per row, tagged with its table: ontology registry,
raw documents, nodes (embeddings included), edges, and mentions. `load`
restores into an empty graph (a fresh init_db database) and refuses to run
over existing data. Ontology rows collide with the seed by design and are
skipped on conflict.

Html: one self-contained file — embedded graph JSON, inline force-directed
canvas layout, no external requests. Whole graph by default, or a bounded
neighborhood with --node/--depth.
"""

import argparse
import json
import sys
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Edge, Node, NodeMention, RawDocument
from app.db.models.ontology import OntologyNodeType, OntologyRelationshipType
from app.db.session import SessionLocal

# Dump/load order satisfies foreign keys: ontology before nodes and edges,
# documents before mentions. Alias nodes are ordered after their canonicals
# at load time.
_MODELS = [OntologyNodeType, OntologyRelationshipType, RawDocument, Node, Edge, NodeMention]
_GRAPH_MODELS = [RawDocument, Node, Edge, NodeMention]


def _encode(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    return [float(x) for x in value]  # pgvector embedding


def _decode(table, row: dict) -> dict:
    out = {}
    for col in table.columns:
        if col.name not in row:
            continue
        value = row[col.name]
        if value is None:
            out[col.name] = None
        elif isinstance(col.type, UUID):
            out[col.name] = uuid.UUID(value)
        elif isinstance(col.type, TIMESTAMP):
            out[col.name] = datetime.fromisoformat(value)
        else:
            out[col.name] = value
    return out


def cmd_dump(db, path: str) -> int:
    counts = {}
    with open(path, "w", encoding="utf-8") as fh:
        for model in _MODELS:
            table = model.__table__
            rows = db.execute(select(table)).mappings().all()
            for row in rows:
                record = {"table": table.name, "row": {k: _encode(v) for k, v in row.items()}}
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts[table.name] = len(rows)
    print(", ".join(f"{name}: {n}" for name, n in counts.items()))
    print(f"dumped to {path}")
    return 0


def cmd_load(db, path: str) -> int:
    for model in _GRAPH_MODELS:
        if db.scalar(select(func.count()).select_from(model.__table__)):
            print(
                f"error: refusing to load — {model.__tablename__} is not empty. "
                "Restore into a fresh database.",
                file=sys.stderr,
            )
            return 1

    by_table: dict[str, list] = {m.__table__.name: [] for m in _MODELS}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record["table"] not in by_table:
                print(f"error: unknown table '{record['table']}' in dump.", file=sys.stderr)
                return 1
            by_table[record["table"]].append(record["row"])

    counts = {}
    for model in _MODELS:
        table = model.__table__
        rows = [_decode(table, r) for r in by_table[table.name]]
        if table.name.startswith("ontology_"):
            # init_db already seeded the registry; only drift-added terms land.
            for row in rows:
                db.execute(pg_insert(table).values(**row).on_conflict_do_nothing())
        elif rows:
            if table.name == "nodes":
                rows.sort(key=lambda r: r.get("canonical_node_id") is not None)
            db.execute(table.insert(), rows)
        counts[table.name] = len(rows)
    db.commit()
    print(", ".join(f"{name}: {n}" for name, n in counts.items()))
    print(f"loaded from {path}")
    return 0


_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>lorekeeper graph</title>
<style>
html,body{margin:0;height:100%;background:#111;color:#ddd;font:13px monospace;overflow:hidden}
#info{position:fixed;top:8px;left:8px;max-width:360px;background:rgba(20,20,20,.92);
padding:10px;border:1px solid #444;border-radius:4px;white-space:pre-wrap;display:none}
#legend{position:fixed;bottom:8px;left:8px;background:rgba(20,20,20,.92);
padding:8px;border:1px solid #444;border-radius:4px}
canvas{display:block}
</style></head>
<body>
<div id="info"></div><div id="legend"></div>
<canvas id="c"></canvas>
<script>
const data = __DATA__;
const canvas = document.getElementById("c"), ctx = canvas.getContext("2d");
function resize(){ canvas.width = innerWidth; canvas.height = innerHeight; }
resize(); addEventListener("resize", resize);
const W = () => canvas.width, H = () => canvas.height;
function hue(t){ let h = 0; for (const ch of t) h = (h*31 + ch.charCodeAt(0)) % 360; return h; }
const nodes = data.nodes.map(n => ({...n,
  x: W()/2 + (Math.random()-.5)*W()*.8, y: H()/2 + (Math.random()-.5)*H()*.8, vx: 0, vy: 0}));
const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
const edges = data.edges.filter(e => byId[e.source] && byId[e.target]);
let alpha = 1;
function tick(){
  for (let i = 0; i < nodes.length; i++) for (let j = i+1; j < nodes.length; j++){
    const a = nodes[i], b = nodes[j];
    let dx = a.x-b.x, dy = a.y-b.y; const d2 = dx*dx + dy*dy || 1;
    const d = Math.sqrt(d2), f = 1200/d2; dx /= d; dy /= d;
    a.vx += dx*f; a.vy += dy*f; b.vx -= dx*f; b.vy -= dy*f;
  }
  for (const e of edges){
    const a = byId[e.source], b = byId[e.target];
    let dx = b.x-a.x, dy = b.y-a.y; const d = Math.sqrt(dx*dx + dy*dy) || 1;
    const f = (d-90)*.02; dx /= d; dy /= d;
    a.vx += dx*f; a.vy += dy*f; b.vx -= dx*f; b.vy -= dy*f;
  }
  for (const n of nodes){
    n.vx += (W()/2 - n.x)*.0015; n.vy += (H()/2 - n.y)*.0015;
    n.x += n.vx*alpha; n.y += n.vy*alpha; n.vx *= .85; n.vy *= .85;
  }
  alpha = Math.max(alpha*.995, .02);
}
function draw(){
  ctx.clearRect(0, 0, W(), H());
  ctx.strokeStyle = "#555";
  for (const e of edges){
    const a = byId[e.source], b = byId[e.target];
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  }
  for (const n of nodes){
    ctx.beginPath(); ctx.fillStyle = `hsl(${hue(n.type)},65%,55%)`;
    ctx.arc(n.x, n.y, 6, 0, 7); ctx.fill();
    ctx.fillStyle = "#ccc"; ctx.fillText(n.name, n.x+8, n.y+4);
  }
}
(function loop(){ tick(); draw(); requestAnimationFrame(loop); })();
canvas.addEventListener("click", ev => {
  const info = document.getElementById("info");
  const hit = nodes.find(n => {
    const dx = n.x-ev.clientX, dy = n.y-ev.clientY; return dx*dx + dy*dy < 100;
  });
  if (!hit){ info.style.display = "none"; return; }
  info.style.display = "block";
  info.textContent = hit.name + "  (" + hit.type + ")\\nid=" + hit.id +
    (hit.summary ? "\\n\\n" + hit.summary : "") +
    "\\n\\n" + JSON.stringify(hit.properties, null, 2);
});
const types = [...new Set(nodes.map(n => n.type))];
document.getElementById("legend").innerHTML = types.map(t =>
  `<span style="color:hsl(${hue(t)},65%,55%)">&#9679;</span> ${t}`).join("&nbsp;&nbsp;");
</script></body></html>
"""


def _neighborhood(db, start_id: uuid.UUID, depth: int):
    """Node ids and edges within `depth` hops of a start node (both directions)."""
    seen = {start_id}
    frontier = {start_id}
    edges: list[tuple] = []
    edge_seen: set[tuple] = set()
    for _ in range(depth):
        if not frontier:
            break
        rows = db.execute(
            select(Edge.source_id, Edge.target_id, Edge.relationship_type).where(
                Edge.source_id.in_(frontier) | Edge.target_id.in_(frontier)
            )
        ).all()
        next_frontier: set = set()
        for s, t, r in rows:
            key = (s, t, r)
            if key not in edge_seen:
                edge_seen.add(key)
                edges.append(key)
            for nid in (s, t):
                if nid not in seen:
                    seen.add(nid)
                    next_frontier.add(nid)
        frontier = next_frontier
    return seen, edges


def cmd_html(db, path: str, node_id: str | None, depth: int, max_nodes: int) -> int:
    if node_id:
        try:
            start = uuid.UUID(node_id)
        except ValueError:
            print(f"error: '{node_id}' is not a valid UUID.", file=sys.stderr)
            return 1
        if db.get(Node, start) is None:
            print(f"error: no node with id {node_id}.", file=sys.stderr)
            return 1
        ids, edge_rows = _neighborhood(db, start, max(1, depth))
        nodes = db.scalars(select(Node).where(Node.id.in_(ids))).all()
    else:
        nodes = db.scalars(select(Node).where(Node.canonical_node_id.is_(None))).all()
        edge_rows = db.execute(select(Edge.source_id, Edge.target_id, Edge.relationship_type)).all()

    if len(nodes) > max_nodes:
        print(f"note: rendering the first {max_nodes} of {len(nodes)} nodes.", file=sys.stderr)
        nodes = nodes[:max_nodes]
    kept = {n.id for n in nodes}
    payload = {
        "nodes": [
            {
                "id": str(n.id),
                "name": n.name,
                "type": n.node_type,
                "summary": n.summary,
                "properties": n.properties,
            }
            for n in nodes
        ],
        "edges": [
            {"source": str(s), "target": str(t), "rel": r}
            for s, t, r in edge_rows
            if s in kept and t in kept
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        fh.write(_HTML.replace("__DATA__", data_json))
    print(f"{len(payload['nodes'])} nodes, {len(payload['edges'])} edges -> {path}")
    return 0


def _main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.export", description="Dump, restore, or render the knowledge graph."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_dump = sub.add_parser("dump", help="Write the whole graph to a JSONL file.")
    p_dump.add_argument("path")
    p_load = sub.add_parser("load", help="Restore a dump into an empty database.")
    p_load.add_argument("path")
    p_html = sub.add_parser("html", help="Render the graph as a self-contained HTML viewer.")
    p_html.add_argument("path")
    p_html.add_argument("--node", help="Center the view on this node id (whole graph if omitted).")
    p_html.add_argument("--depth", type=int, default=2, help="Hops out from --node (default 2).")
    p_html.add_argument(
        "--max-nodes", type=int, default=2000, help="Cap on rendered nodes (default 2000)."
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        if args.cmd == "dump":
            return cmd_dump(db, args.path)
        if args.cmd == "html":
            return cmd_html(db, args.path, args.node, args.depth, args.max_nodes)
        return cmd_load(db, args.path)


if __name__ == "__main__":
    raise SystemExit(_main())
