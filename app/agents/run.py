"""Maintenance runner — one shot of graph grooming.

    python -m app.agents.run                 # run every registered agent
    python -m app.agents.run --agent dedup   # run one

Cron-able: each run scans, files proposals (dedup keys make re-runs
idempotent), and prints a JSON report. Review the queue with
`python -m app.review list`.
"""

import json
import logging
import sys

from app.agents import AgentFactory
from app.db.init_db import init_db
from app.db.session import SessionLocal

logger = logging.getLogger("company_brain.agents.run")


def run_agents(db, names: list[str]) -> dict:
    report: dict = {"agents": {}, "proposals_filed": 0}
    for name in names:
        filed = AgentFactory.create(db, name).scan()
        report["agents"][name] = [
            {
                "id": str(p.id),
                "kind": p.kind,
                "status": p.status,
                "confidence": p.confidence,
            }
            for p in filed
        ]
        report["proposals_filed"] += len(filed)
    return report


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.agents.run",
        description="Scan the graph with maintenance agents and file proposals.",
    )
    parser.add_argument(
        "--agent",
        choices=AgentFactory.available(),
        help="Run a single agent (default: all of them).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    init_db()
    names = [args.agent] if args.agent else AgentFactory.available()
    with SessionLocal() as db:
        report = run_agents(db, names)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
