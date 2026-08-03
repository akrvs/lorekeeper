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
        "job",
        nargs="?",
        choices=["digest", "importance"],
        help="Run a standalone job instead of the proposal-filing agents.",
    )
    parser.add_argument(
        "--agent",
        choices=AgentFactory.available(),
        help="Run a single agent (default: all of them).",
    )
    parser.add_argument("--days", type=int, default=7, help="Window for the digest job.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    init_db()
    with SessionLocal() as db:
        if args.job == "digest":
            from app.agents.digest import build_digest

            print(build_digest(db, args.days))
            return 0
        if args.job == "importance":
            from app.agents.importance import compute_importance

            print(f"importance stamped on {compute_importance(db)} node(s)")
            return 0
        names = [args.agent] if args.agent else AgentFactory.available()
        report = run_agents(db, names)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
