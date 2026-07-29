"""1-click offline demo runner (Phase 9) — invoked by `make demo`.

Ingests demo_vault/ via the LocalConnector with the offline stub provider, then
prints a clean summary of the resolved knowledge graph and example MCP queries.
Proves that every `[[wikilink]]` resolved to a real file node (zero abstract
concept nodes).
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.db.init_db import init_db
from app.db.models import Edge, Node
from app.db.session import SessionLocal
from app.pipeline import run_source

BAR = "=" * 66
RULE = "-" * 66


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    # Mute migration/pipeline INFO chatter for a clean demo (fileConfig in the
    # Alembic env would otherwise re-enable it, so use the global disable switch).
    logging.disable(logging.INFO)
    try:
        init_db()
        with SessionLocal() as db:
            report = run_source(db, "local")
    finally:
        logging.disable(logging.NOTSET)

    with SessionLocal() as db:
        files = db.scalars(
            select(Node).where(Node.source_system == "local").order_by(Node.name)
        ).all()
        concept_count = db.scalar(
            select(func.count())
            .select_from(Node)
            .where(Node.source_system.is_(None), Node.node_type == "document")
        )
        src, tgt = aliased(Node), aliased(Node)
        edges = db.execute(
            select(src.name, tgt.name)
            .join(Edge, Edge.source_id == src.id)
            .join(tgt, tgt.id == Edge.target_id)
            .where(Edge.relationship_type == "REFERENCES")
            .order_by(src.name, tgt.name)
        ).all()

    out_by_src: dict[str, list[str]] = {}
    for s, t in edges:
        out_by_src.setdefault(s, []).append(t)

    resolved_ok = (
        "OK: all [[wikilinks]] resolved to files" if concept_count == 0 else "WARN: unresolved"
    )

    lines = [
        "",
        BAR,
        "  Company Brain — 1-Click Offline Demo  (LLM_PROVIDER=stub)",
        BAR,
        "",
        f"  Vault             : demo_vault/  ({report['documents']} markdown files)",
        f"  File nodes        : {len(files)}",
        f"  Concept nodes     : {concept_count}   {resolved_ok}",
        f"  REFERENCES edges  : {len(edges)}",
        f"  Wikilinks resolved: {report.get('wikilinks_resolved', 0)}",
        "",
        "  Resolved knowledge graph (every node is a real file — no abstractions):",
        "",
    ]
    for f in files:
        lines.append(f"   [file] {f.name}")
        for target in out_by_src.get(f.name, []):
            lines.append(f"          └─[REFERENCES]──▶ {target}")
    lines += [
        "",
        RULE,
        "Demo ingested successfully! Here are 3 example MCP queries you can now",
        "test in Cursor/Claude Desktop to see the graph in action:",
        "",
        '  1. "Search Company Brain for the checkout-v2 architecture, then show',
        '      me which documents reference it."',
        '  2. "What does the incident report connect to? Show its neighbors."',
        '  3. "Trace a path from the incident report to the api-keys-policy doc."',
        "",
        "  (Point your MCP client at this demo database — see docs/MCP_SETUP.md.)",
        BAR,
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
