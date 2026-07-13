"""Shared test fixtures.

Drives the REAL async connectors offline by injecting `httpx.MockTransport`
handlers that replay realistic GitHub/Slack API payloads. The replayed data is
wired to the same checkout-v2 / deploy-failure scenario, with external_ids that
match the scripted extraction below, so the full pipeline (connector -> extract
-> embed -> resolve) runs deterministically without network or Azure.
"""

import re

import httpx
from sqlalchemy import text

from app.cache import get_cache
from app.config import settings
from app.connectors import GitHubConnector, SlackConnector
from app.llm.stub import deterministic_embedding
from app.ontology.schema import (
    ExtractedEdge,
    ExtractedNode,
    ExtractionResult,
    NodeTypeEnum,
    PropertyKV,
    RelationshipTypeEnum,
)
from app.pipeline import process_document

# --------------------------------------------------------------------------- #
# Replayed API payloads
# --------------------------------------------------------------------------- #
_PR_142 = {
    "number": 142,
    "title": "Add one-click checkout (checkout-v2)",
    "state": "closed",
    "merged_at": "2026-05-20T14:03:00Z",
    "body": "Implements the checkout-v2 feature. Prod rollout crash-looped on a "
    "missing PAYMENTS_API_KEY — deploy failure for acme/checkout-service.",
    "user": {"login": "alice"},
    "html_url": "https://github.com/acme/checkout-service/pull/142",
    "created_at": "2026-05-20T13:00:00Z",
    "updated_at": "2026-05-20T14:03:00Z",
}
_ISSUE_143 = {
    "number": 143,
    "title": "checkout-v2 deploy failure in production",
    "state": "open",
    "body": "The 2026-05-20 deploy of acme/checkout-service failed; tied to checkout-v2 (PR #142).",
    "user": {"login": "bob"},
    "comments": 1,
    "html_url": "https://github.com/acme/checkout-service/issues/143",
    "created_at": "2026-05-20T14:40:00Z",
    "updated_at": "2026-05-20T14:41:00Z",
}
# The issues endpoint also returns PRs; this one must be filtered out.
_PR_142_AS_ISSUE = {**_PR_142, "pull_request": {"url": _PR_142["html_url"]}}
_ISSUE_143_COMMENTS = [
    {"user": {"login": "bob"}, "body": "Confirmed: checkout-v2 caused the deploy failure."},
]

_SLACK_USERS = [
    {"id": "U_ALICE", "name": "alice", "profile": {"display_name": "alice"}},
    {"id": "U_BOB", "name": "bob", "profile": {"display_name": "bob"}},
]
_THREAD_TS = "1747749900.000100"
_SLACK_HISTORY = [
    {
        "type": "message",
        "user": "U_BOB",
        "ts": _THREAD_TS,
        "thread_ts": _THREAD_TS,
        "reply_count": 3,
        "text": "prod checkout is down — pods crash-looping after the deploy",
    },
]
_SLACK_REPLIES = [
    {"user": "U_BOB", "ts": _THREAD_TS, "text": "prod checkout is down after the deploy"},
    {
        "user": "U_ALICE",
        "ts": "1747749960.000200",
        "text": "that's the checkout-v2 rollout (PR #142), missing PAYMENTS_API_KEY",
    },
    {
        "user": "U_BOB",
        "ts": "1747749980.000300",
        "text": "so checkout-v2 caused the deployment failure?",
    },
    {"user": "U_ALICE", "ts": "1747750020.000400", "text": "yep, rolling forward with the fix"},
]


def _github_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/pulls"):
        return httpx.Response(200, json=[_PR_142])
    if path.endswith("/issues"):
        return httpx.Response(200, json=[_ISSUE_143, _PR_142_AS_ISSUE])
    if path.endswith("/issues/143/comments"):
        return httpx.Response(200, json=_ISSUE_143_COMMENTS)
    return httpx.Response(404, json={"message": f"not found: {path}"})


def _slack_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    meta = {"response_metadata": {"next_cursor": ""}}
    if path.endswith("/users.list"):
        return httpx.Response(200, json={"ok": True, "members": _SLACK_USERS, **meta})
    if path.endswith("/conversations.history"):
        return httpx.Response(200, json={"ok": True, "messages": _SLACK_HISTORY, **meta})
    if path.endswith("/conversations.replies"):
        return httpx.Response(200, json={"ok": True, "messages": _SLACK_REPLIES, **meta})
    return httpx.Response(200, json={"ok": False, "error": "unknown_method"})


def make_github_connector(db):
    return GitHubConnector(
        db,
        repo="acme/checkout-service",
        token="test-token",
        transport=httpx.MockTransport(_github_handler),
    )


def make_slack_connector(db):
    return SlackConnector(
        db,
        channel_id="C0INCIDENTS",
        token="test-token",
        transport=httpx.MockTransport(_slack_handler),
    )


# --------------------------------------------------------------------------- #
# Scripted LLM (stands in for Azure structured output)
# --------------------------------------------------------------------------- #
def N(temp_id, ntype, name, summary, *, source=None, ext=None, **props):
    return ExtractedNode(
        temp_id=temp_id,
        node_type=NodeTypeEnum(ntype),
        name=name,
        summary=summary,
        source_system=source,
        external_id=ext,
        properties=[PropertyKV(key=k, value=str(v)) for k, v in props.items()],
        confidence=0.95,
    )


def E(src, tgt, rel):
    return ExtractedEdge(
        source_temp_id=src,
        target_temp_id=tgt,
        relationship=RelationshipTypeEnum(rel),
        confidence=0.9,
    )


SCRIPT: dict[tuple[str, str], ExtractionResult] = {
    ("github", "142"): ExtractionResult(
        nodes=[
            N("n1", "user", "alice", "GitHub user alice.", source="github", ext="alice"),
            N(
                "n2",
                "repository",
                "acme/checkout-service",
                "The checkout service repo.",
                source="github",
                ext="acme/checkout-service",
            ),
            N(
                "n3",
                "pull_request",
                "PR #142: Add one-click checkout (checkout-v2)",
                "PR implementing the checkout-v2 feature.",
                source="github",
                ext="142",
            ),
            N("n4", "feature", "checkout-v2", "One-click checkout flow replacing the legacy cart."),
            N(
                "n5",
                "incident",
                "checkout-v2 deploy failure",
                "Production deploy crash-looped on a missing PAYMENTS_API_KEY.",
                status="resolved",
            ),
        ],
        edges=[
            E("n1", "n3", "AUTHORED"),
            E("n3", "n2", "PART_OF"),
            E("n3", "n4", "IMPLEMENTS"),
            E("n3", "n5", "CAUSED"),
            E("n4", "n5", "CAUSED"),
            E("n5", "n2", "AFFECTS"),
        ],
    ),
    ("github", "143"): ExtractionResult(
        nodes=[
            N("n1", "user", "bob", "GitHub user bob.", source="github", ext="bob"),
            N(
                "n2",
                "repository",
                "acme/checkout-service",
                "The checkout service repo.",
                source="github",
                ext="acme/checkout-service",
            ),
            N(
                "n3",
                "issue",
                "Issue #143: checkout-v2 deploy failure",
                "Issue tracking the production deploy failure.",
                source="github",
                ext="143",
            ),
            N(
                "n4",
                "incident",
                "checkout-v2 deploy failure",
                "Production deploy of checkout-service crash-looped during the checkout-v2 rollout.",
            ),
        ],
        edges=[E("n1", "n3", "AUTHORED"), E("n3", "n2", "PART_OF"), E("n3", "n4", "RELATES_TO")],
    ),
    ("slack", _THREAD_TS): ExtractionResult(
        nodes=[
            N(
                "n1",
                "slack_thread",
                "#incidents thread: checkout outage",
                "Thread discussing the checkout-v2 deploy failure.",
                source="slack",
                ext=_THREAD_TS,
            ),
            N(
                "n2",
                "slack_channel",
                "incidents",
                "The #incidents Slack channel.",
                source="slack",
                ext="C0INCIDENTS",
            ),
            N("n3", "feature", "checkout-v2", "One-click checkout flow rollout."),
            N(
                "n4",
                "incident",
                "checkout-v2 deploy failure",
                "Production checkout-service pods crash-looped after the checkout-v2 deploy.",
            ),
        ],
        edges=[E("n1", "n2", "POSTED_IN"), E("n1", "n3", "DISCUSSES"), E("n1", "n4", "DISCUSSES")],
    ),
}


class ScriptedProvider:
    """Deterministic embeddings + scripted extraction keyed by (source, external_id)."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [deterministic_embedding(t, settings.embedding_dim) for t in texts]

    def extract(self, system_prompt: str, user_content: str, schema):
        src = re.search(r"^source_system: (.+)$", user_content, re.M).group(1).strip()
        ext = re.search(r"^external_id: (.+)$", user_content, re.M).group(1).strip()
        return SCRIPT[(src, ext)]


def populate(db, provider) -> None:
    """Deterministically (re)build the demo graph via the real connectors."""
    db.execute(
        text(
            "TRUNCATE node_mentions, edges, nodes, raw_documents, ingestion_runs RESTART IDENTITY CASCADE"
        )
    )
    db.commit()
    for connector in (make_github_connector(db), make_slack_connector(db)):
        _run, docs = connector.run()
        for doc in docs:
            process_document(db, provider, doc)
    get_cache().bump("graph")  # invalidate any cached reads after a rebuild
