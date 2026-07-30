import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db.models import RawDocument
from app.db.session import get_db
from app.main import app

_GH_SECRET = "gh-test-secret"
_SLACK_SECRET = "slack-test-secret"


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", _GH_SECRET)
    monkeypatch.setattr(settings, "slack_signing_secret", _SLACK_SECRET)

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _gh_headers(body: bytes, event: str) -> dict:
    sig = "sha256=" + hmac.new(_GH_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": sig, "X-GitHub-Event": event}


def _slack_headers(body: bytes) -> dict:
    ts = str(int(time.time()))
    base = f"v0:{ts}:{body.decode()}"
    sig = "v0=" + hmac.new(_SLACK_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def test_github_webhook_rejects_bad_signature(client):
    body = json.dumps({"zen": "ok"}).encode()
    res = client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "issues"},
    )
    assert res.status_code == 401


def test_github_webhook_stores_issue(client, db):
    payload = {
        "repository": {"full_name": "acme/checkout-service"},
        "issue": {
            "number": 555,
            "title": "webhook issue",
            "state": "open",
            "body": "arrived by push",
            "user": {"login": "alice"},
            "html_url": "https://github.com/acme/checkout-service/issues/555",
            "created_at": "2026-07-30T10:00:00Z",
            "updated_at": "2026-07-30T10:00:00Z",
        },
    }
    body = json.dumps(payload).encode()
    res = client.post("/webhooks/github", content=body, headers=_gh_headers(body, "issues"))
    assert res.status_code == 200 and res.json()["status"] == "stored"
    doc = db.scalar(
        select(RawDocument).where(
            RawDocument.source_system == "github", RawDocument.external_id == "555"
        )
    )
    assert doc is not None and "arrived by push" in doc.content


def test_github_webhook_ignores_unhandled_events(client):
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    res = client.post("/webhooks/github", content=body, headers=_gh_headers(body, "push"))
    assert res.status_code == 200 and res.json()["status"] == "ignored"


def test_slack_url_verification_challenge(client):
    body = json.dumps({"type": "url_verification", "challenge": "c123"}).encode()
    res = client.post("/webhooks/slack", content=body, headers=_slack_headers(body))
    assert res.status_code == 200 and res.json() == {"challenge": "c123"}


def test_slack_webhook_stores_message(client, db):
    payload = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C42",
            "ts": "1753862400.000100",
            "user": "U1",
            "text": "deploy went fine",
        },
    }
    body = json.dumps(payload).encode()
    res = client.post("/webhooks/slack", content=body, headers=_slack_headers(body))
    assert res.status_code == 200 and res.json()["status"] == "stored"
    doc = db.scalar(
        select(RawDocument).where(
            RawDocument.source_system == "slack",
            RawDocument.external_id == "C42:1753862400.000100",
        )
    )
    assert doc is not None and doc.content == "deploy went fine"


def test_slack_webhook_rejects_stale_timestamp(client):
    body = json.dumps({"type": "url_verification", "challenge": "x"}).encode()
    ts = str(int(time.time()) - 3600)
    base = f"v0:{ts}:{body.decode()}"
    sig = "v0=" + hmac.new(_SLACK_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
    res = client.post(
        "/webhooks/slack",
        content=body,
        headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig},
    )
    assert res.status_code == 401


def test_webhooks_unconfigured_secret_is_503(client, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", None)
    res = client.post("/webhooks/github", content=b"{}")
    assert res.status_code == 503


def test_metrics_exposes_prometheus_counters(client):
    res = client.get("/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "lorekeeper_nodes_total " in res.text
    assert "lorekeeper_edges_total " in res.text
    assert "lorekeeper_raw_documents_total " in res.text
