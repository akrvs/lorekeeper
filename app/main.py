"""FastAPI entrypoint.

This process is the backend/control plane (health, future ingestion triggers,
admin). The agent-facing surface is the separate MCP server (Step 3). On
startup it bootstraps the schema so `docker compose up` yields a ready system.
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.cache import get_cache
from app.config import settings
from app.connectors import ConnectorAuthError, ConnectorError, ConnectorFactory
from app.connectors.base import BaseConnector, RawDoc
from app.connectors.github import _iso
from app.connectors.jira import _ts as _jira_ts
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


@app.get("/metrics", tags=["meta"])
def metrics(db: Session = Depends(get_db)) -> PlainTextResponse:
    """Prometheus metrics in text exposition format."""
    from app.db.models import Proposal
    from app.db.models.source import IngestionRun, RawDocument

    lines = [
        f"lorekeeper_nodes_total {db.scalar(select(func.count()).select_from(Node))}",
        f"lorekeeper_edges_total {db.scalar(select(func.count()).select_from(Edge))}",
        "lorekeeper_raw_documents_total "
        f"{db.scalar(select(func.count()).select_from(RawDocument))}",
    ]
    runs = db.execute(select(IngestionRun.status, func.count()).group_by(IngestionRun.status))
    lines += [f'lorekeeper_ingestion_runs_total{{status="{s}"}} {c}' for s, c in runs]
    proposals = db.execute(select(Proposal.status, func.count()).group_by(Proposal.status))
    lines += [f'lorekeeper_proposals_total{{status="{s}"}} {c}' for s, c in proposals]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


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


class _WebhookSink(BaseConnector):
    def __init__(self, db: Session, source: str):
        super().__init__(db)
        self.source_system = source

    def fetch(self) -> list[RawDoc]:
        return []

    def store(self, raw: RawDoc):
        document = self._upsert_document(raw)
        self.db.commit()
        get_cache().bump("graph")
        return document


def _verify_github_signature(body: bytes, signature: str | None) -> bool:
    expected = (
        "sha256="
        + hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    )
    return signature is not None and secrets.compare_digest(expected, signature)


def _github_doc(event: str, payload: dict) -> RawDoc | None:
    repo = (payload.get("repository") or {}).get("full_name")
    if event == "pull_request" and payload.get("pull_request"):
        pr = payload["pull_request"]
        state = "merged" if pr.get("merged_at") else pr.get("state", "unknown")
        return RawDoc(
            source_type="pull_request",
            external_id=str(pr["number"]),
            resource_key=repo,
            title=pr.get("title"),
            url=pr.get("html_url"),
            author=(pr.get("user") or {}).get("login"),
            content=(
                f"PR #{pr['number']}: {pr.get('title', '')}"
                f"\nState: {state}\n\n{pr.get('body') or ''}"
            ),
            raw_payload=pr,
            source_created_at=_iso(pr.get("created_at")),
            source_updated_at=_iso(pr.get("updated_at")),
        )
    if event in ("issues", "issue_comment") and payload.get("issue"):
        issue = payload["issue"]
        if "pull_request" in issue:
            return None
        content = (
            f"Issue #{issue['number']}: {issue.get('title', '')}"
            f"\nState: {issue.get('state')}\n\n{issue.get('body') or ''}"
        )
        comment = payload.get("comment")
        if event == "issue_comment" and comment:
            author = (comment.get("user") or {}).get("login", "unknown")
            content += f"\n\ncomment by {author}: {comment.get('body', '')}"
        return RawDoc(
            source_type="issue",
            external_id=str(issue["number"]),
            resource_key=repo,
            title=issue.get("title"),
            url=issue.get("html_url"),
            author=(issue.get("user") or {}).get("login"),
            content=content,
            raw_payload=payload,
            source_created_at=_iso(issue.get("created_at")),
            source_updated_at=_iso(issue.get("updated_at")),
        )
    return None


@app.post("/webhooks/github", tags=["ingestion"])
async def github_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """Push-based GitHub ingestion (issues, pull_request, issue_comment events).

    Requires GITHUB_WEBHOOK_SECRET; the X-Hub-Signature-256 header is verified
    against the raw body. Matching events land in raw_documents immediately and
    are extracted on the next pipeline run.
    """
    if not settings.github_webhook_secret:
        raise HTTPException(503, "GITHUB_WEBHOOK_SECRET is not configured.")
    body = await request.body()
    if not _verify_github_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Invalid webhook signature.")
    event = request.headers.get("X-GitHub-Event", "")
    doc = _github_doc(event, json.loads(body))
    if doc is None:
        return {"status": "ignored", "event": event}
    document = await run_in_threadpool(_WebhookSink(db, "github").store, doc)
    return {"status": "stored", "document_id": str(document.id)}


def _verify_slack_signature(body: bytes, timestamp: str | None, signature: str | None) -> bool:
    try:
        if not timestamp or abs(time.time() - float(timestamp)) > 300:
            return False
        base = f"v0:{timestamp}:{body.decode('utf-8')}"
    except (ValueError, UnicodeDecodeError):
        return False
    expected = (
        "v0="
        + hmac.new(
            settings.slack_signing_secret.encode(), base.encode(), hashlib.sha256
        ).hexdigest()
    )
    return signature is not None and secrets.compare_digest(expected, signature)


@app.post("/webhooks/slack", tags=["ingestion"])
async def slack_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """Push-based Slack ingestion (Events API message events).

    Requires SLACK_SIGNING_SECRET; the X-Slack-Signature header is verified
    against the raw body. Answers url_verification challenges; message events
    land in raw_documents immediately and are extracted on the next pipeline run.
    """
    if not settings.slack_signing_secret:
        raise HTTPException(503, "SLACK_SIGNING_SECRET is not configured.")
    body = await request.body()
    if not _verify_slack_signature(
        body,
        request.headers.get("X-Slack-Request-Timestamp"),
        request.headers.get("X-Slack-Signature"),
    ):
        raise HTTPException(401, "Invalid webhook signature.")
    payload = json.loads(body)
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    event = payload.get("event") or {}
    if payload.get("type") != "event_callback" or event.get("type") != "message":
        return {"status": "ignored"}
    if event.get("subtype") or not event.get("ts"):
        return {"status": "ignored"}
    channel = event.get("channel")
    stamp = datetime.fromtimestamp(float(event["ts"]), UTC)
    doc = RawDoc(
        source_type="message",
        external_id=f"{channel}:{event['ts']}",
        resource_key=channel,
        author=event.get("user"),
        content=event.get("text") or "",
        raw_payload=event,
        source_created_at=stamp,
        source_updated_at=stamp,
    )
    document = await run_in_threadpool(_WebhookSink(db, "slack").store, doc)
    return {"status": "stored", "document_id": str(document.id)}


def _jira_doc(payload: dict) -> RawDoc | None:
    issue = payload.get("issue")
    if not issue or not issue.get("key"):
        return None
    key = issue["key"]
    fields = issue.get("fields") or {}
    status = ((fields.get("status") or {}).get("name")) or "unknown"
    summary = fields.get("summary") or ""
    content = f"Jira {key}: {summary}\nStatus: {status}\n\n{fields.get('description') or ''}"
    comment = payload.get("comment")
    if comment:
        author = (comment.get("author") or {}).get("displayName", "unknown")
        content += f"\n\ncomment by {author}: {comment.get('body', '')}"
    base = (issue.get("self") or "").split("/rest/")[0] or (settings.jira_base_url or "").rstrip(
        "/"
    )
    return RawDoc(
        source_type="jira_issue",
        external_id=key,
        resource_key=((fields.get("project") or {}).get("key")) or key.split("-")[0],
        title=summary or key,
        url=f"{base}/browse/{key}" if base else None,
        author=(fields.get("creator") or {}).get("displayName"),
        content=content,
        raw_payload=issue,
        source_created_at=_jira_ts(fields.get("created")),
        source_updated_at=_jira_ts(fields.get("updated")),
    )


@app.post("/webhooks/jira", tags=["ingestion"])
async def jira_webhook(
    request: Request,
    token: str | None = None,
    x_jira_webhook_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """Push-based Jira ingestion (issue and comment events).

    Jira Cloud does not sign webhook payloads, so the route is gated by a
    shared secret. Prefer registering the webhook with an
    X-Jira-Webhook-Token header (Jira Cloud sends custom headers); the
    ?token=JIRA_WEBHOOK_SECRET query form still works but ends up in proxy
    and access logs. Issue events land in raw_documents immediately and are
    extracted on the next pipeline run.
    """
    if not settings.jira_webhook_secret:
        raise HTTPException(503, "JIRA_WEBHOOK_SECRET is not configured.")
    supplied = x_jira_webhook_token or token
    if not (supplied and secrets.compare_digest(supplied, settings.jira_webhook_secret)):
        raise HTTPException(401, "Invalid or missing token.")
    try:
        payload = json.loads(await request.body())
    except ValueError as exc:
        raise HTTPException(400, "Malformed JSON body.") from exc
    doc = _jira_doc(payload)
    if doc is None:
        return {"status": "ignored", "event": payload.get("webhookEvent")}
    document = await run_in_threadpool(_WebhookSink(db, "jira").store, doc)
    return {"status": "stored", "document_id": str(document.id)}


def _verify_notion_signature(body: bytes, signature: str | None) -> bool:
    expected = (
        "sha256="
        + hmac.new(settings.notion_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    )
    return signature is not None and secrets.compare_digest(expected, signature)


def _notion_resync() -> None:
    """Notion webhook events carry ids, not content — pull the pages instead."""
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        try:
            run_source(db, "notion")
        except Exception as exc:  # noqa: BLE001 — a failed resync must not crash the worker
            logger.warning("Notion webhook-triggered sync failed: %s", exc)


@app.post("/webhooks/notion", tags=["ingestion"])
async def notion_webhook(request: Request, background: BackgroundTasks) -> dict:
    """Push-based Notion freshness (page/database change events).

    On subscription Notion POSTs a one-time verification_token - it is echoed
    (masked) so the operator can confirm it matches the value shown in the
    Notion subscription, then set NOTION_WEBHOOK_SECRET to it out of band.
    Never copy the token from server logs: until the secret is configured this
    endpoint is unauthenticated, so anyone could POST their own token. After
    the secret is set, X-Notion-Signature is verified against the raw body.
    Notion events carry ids rather than content, so a verified event schedules
    a targeted connector sync in the background instead of storing the payload.
    """
    if not settings.notion_webhook_secret:
        raise HTTPException(503, "NOTION_WEBHOOK_SECRET is not configured.")
    body = await request.body()
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "Malformed JSON body.") from exc
    if "verification_token" in payload:
        supplied = str(payload["verification_token"])
        masked = supplied[:3] + "..." + supplied[-3:] if len(supplied) > 8 else "***"
        logger.info("Notion webhook verification token received (%s)", masked)
        return {
            "status": (
                "verification received - compare it with the token in your "
                "Notion subscription, then set NOTION_WEBHOOK_SECRET out of band"
            )
        }
    if not _verify_notion_signature(body, request.headers.get("X-Notion-Signature")):
        raise HTTPException(401, "Invalid webhook signature.")
    background.add_task(_notion_resync)
    return {"status": "sync scheduled", "event": payload.get("type")}


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "name": "company-brain",
        "version": __version__,
        "docs": "/docs",
        "note": "Agent tools are exposed via the MCP server (Step 3), not this REST API.",
    }
