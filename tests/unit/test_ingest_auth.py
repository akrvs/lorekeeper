"""INGEST_API_KEY gate on POST /ingest (no DB needed: rejected before the connector runs)."""

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_ingest_rejected_without_or_with_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, "ingest_api_key", "sekret")
    client = TestClient(app)
    assert client.post("/ingest/local").status_code == 401
    assert client.post("/ingest/local", headers={"X-API-Key": "wrong"}).status_code == 401


def test_ingest_correct_key_passes_the_gate(monkeypatch):
    monkeypatch.setattr(settings, "ingest_api_key", "sekret")
    client = TestClient(app)
    resp = client.post("/ingest/does-not-exist", headers={"X-API-Key": "sekret"})
    assert resp.status_code == 404


def test_ingest_open_when_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "ingest_api_key", None)
    client = TestClient(app)
    assert client.post("/ingest/does-not-exist").status_code == 404
