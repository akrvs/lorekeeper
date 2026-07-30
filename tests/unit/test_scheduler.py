import app.scheduler as scheduler
from app.scheduler import configured_sources, run_cycle


def test_configured_sources_filters_unknown(monkeypatch):
    monkeypatch.setattr(scheduler.settings, "sync_sources", "github, nope ,slack,")
    assert configured_sources() == ["github", "slack"]


def test_configured_sources_empty_when_unset(monkeypatch):
    monkeypatch.setattr(scheduler.settings, "sync_sources", None)
    assert configured_sources() == []


def test_run_cycle_isolates_failures_and_grooms(monkeypatch):
    calls = {"agents": None}

    def fake_run_source(db, source, **kwargs):
        if source == "slack":
            raise RuntimeError("upstream down")
        return {"source": source, "documents": 1}

    def fake_run_agents(db, names):
        calls["agents"] = names
        return {"agents": {}, "proposals_filed": 0}

    class FakeDB:
        def rollback(self):
            pass

    import app.pipeline

    monkeypatch.setattr(app.pipeline, "run_source", fake_run_source)
    monkeypatch.setattr(scheduler, "run_agents", fake_run_agents)

    report = run_cycle(FakeDB(), ["github", "slack"])
    assert report["sources"]["github"] == {"source": "github", "documents": 1}
    assert "upstream down" in report["sources"]["slack"]["error"]
    assert calls["agents"] == scheduler.AgentFactory.available()
