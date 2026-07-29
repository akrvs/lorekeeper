"""FastAPI entrypoint.

This process is the backend/control plane (health, future ingestion triggers,
admin). The agent-facing surface is the separate MCP server (Step 3). On
startup it bootstraps the schema so `docker compose up` yields a ready system.
"""

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.config import settings
from app.connectors import ConnectorAuthError, ConnectorError, ConnectorFactory
from app.db.init_db import init_db
from app.db.models import Edge, Node, OntologyNodeType, OntologyRelationshipType
from app.db.session import get_db
from app.pipeline import run_source

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("company_brain")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is owned by Alembic: wait for the DB, apply `upgrade head`, seed
    # the ontology. No SQLAlchemy create_all.
    logger.info("Startup: applying Alembic migrations + seeding ontology...")
    init_db()
    yield


app = FastAPI(
    title="Company Brain",
    version=__version__,
    summary="Organizational memory layer for AI agents (ontology-backed knowledge graph).",
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness — does not touch the DB."""
    return {"status": "ok", "version": __version__}


@app.get("/health/db", tags=["meta"])
def health_db(db: Session = Depends(get_db)) -> dict:
    """Readiness — verifies DB connectivity and reports graph size."""
    nodes = db.scalar(select(func.count()).select_from(Node))
    edges = db.scalar(select(func.count()).select_from(Edge))
    node_types = db.scalar(select(func.count()).select_from(OntologyNodeType))
    rel_types = db.scalar(select(func.count()).select_from(OntologyRelationshipType))
    return {
        "status": "ok",
        "graph": {"nodes": nodes, "edges": edges},
        "ontology": {"node_types": node_types, "relationship_types": rel_types},
    }


@app.post("/ingest/{source}", tags=["ingestion"])
def ingest(
    source: str,
    repo: str | None = None,
    channel_id: str | None = None,
    force: bool = False,
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """Run a connector end-to-end (fetch -> extract -> embed -> resolve).

    Live integration: requires GITHUB_TOKEN / SLACK_BOT_TOKEN and a target
    (`repo` for github, `channel_id` for slack; falls back to GITHUB_REPO /
    SLACK_CHANNEL_ID). With LLM_PROVIDER=azure extraction is real; with =stub it
    only populates raw_documents. If INGEST_API_KEY is set, the matching
    X-API-Key header is required.
    """
    required = settings.ingest_api_key
    if required and not (x_api_key and secrets.compare_digest(x_api_key, required)):
        raise HTTPException(401, "Invalid or missing X-API-Key header.")
    if source not in ConnectorFactory.available():
        raise HTTPException(
            404, f"Unknown source {source!r}. Known: {ConnectorFactory.available()}"
        )
    try:
        return run_source(db, source, repo=repo, channel_id=channel_id, force=force)
    except ValueError as exc:  # misconfiguration (missing repo/token/channel)
        raise HTTPException(400, str(exc)) from exc
    except ConnectorAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except ConnectorError as exc:  # upstream API failure after retries
        raise HTTPException(502, str(exc)) from exc


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "name": "company-brain",
        "version": __version__,
        "docs": "/docs",
        "note": "Agent tools are exposed via the MCP server (Step 3), not this REST API.",
    }
