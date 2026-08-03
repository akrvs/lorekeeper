"""Graph export/import — full-fidelity backup of the knowledge graph.

    python -m app.export dump graph.jsonl
    python -m app.export load graph.jsonl

One JSON line per row, tagged with its table: ontology registry, raw documents,
nodes (embeddings included), edges, and mentions. `load` restores into an empty
graph (a fresh init_db database) and refuses to run over existing data.
Ontology rows collide with the seed by design and are skipped on conflict.
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


def _main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.export", description="Dump or restore the knowledge graph."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_dump = sub.add_parser("dump", help="Write the whole graph to a JSONL file.")
    p_dump.add_argument("path")
    p_load = sub.add_parser("load", help="Restore a dump into an empty database.")
    p_load.add_argument("path")
    args = parser.parse_args()

    with SessionLocal() as db:
        if args.cmd == "dump":
            return cmd_dump(db, args.path)
        return cmd_load(db, args.path)


if __name__ == "__main__":
    raise SystemExit(_main())
