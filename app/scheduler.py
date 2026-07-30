import argparse
import json
import logging
import sys
import time

from sqlalchemy.orm import Session

from app.agents import AgentFactory
from app.agents.run import run_agents
from app.config import settings
from app.connectors import ConnectorFactory
from app.db.init_db import init_db
from app.db.session import SessionLocal

logger = logging.getLogger("company_brain.scheduler")


def configured_sources() -> list[str]:
    configured = settings.sync_sources
    if not configured:
        return []
    known = ConnectorFactory.available()
    sources: list[str] = []
    for name in (s.strip() for s in configured.split(",")):
        if not name:
            continue
        if name not in known:
            logger.warning("SYNC_SOURCES contains unknown source %r (known: %s)", name, known)
            continue
        sources.append(name)
    return sources


def run_cycle(db: Session, sources: list[str]) -> dict:
    from app.pipeline import run_source

    report: dict = {"sources": {}, "agents": None}
    for source in sources:
        try:
            report["sources"][source] = run_source(db, source)
        except Exception as exc:
            logger.exception("Scheduled sync failed for %s", source)
            db.rollback()
            report["sources"][source] = {"error": str(exc)}
    report["agents"] = run_agents(db, AgentFactory.available())
    return report


def _main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scheduler",
        description="Run connector syncs and maintenance agents on an interval.",
    )
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Seconds between cycles (default: SYNC_INTERVAL_SECONDS).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    sources = configured_sources()
    if not sources:
        print("error: SYNC_SOURCES is empty; nothing to schedule", file=sys.stderr)
        return 2

    init_db()
    interval = args.interval or settings.sync_interval_seconds
    logger.info("Scheduler started: sources=%s interval=%ss", sources, interval)
    while True:
        with SessionLocal() as db:
            report = run_cycle(db, sources)
        logger.info("Scheduler cycle report: %s", json.dumps(report, default=str))
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(_main())
