"""Connector resilience against the real async code (httpx.MockTransport)."""

import httpx
from sqlalchemy import select, text

from app.connectors import GitHubConnector, JiraConnector, SlackConnector
from app.db.models import RawDocument

_gh_calls = {"pulls": 0}
_PR = {
    "number": 1,
    "title": "first",
    "state": "open",
    "body": "b",
    "user": {"login": "alice"},
    "html_url": "u1",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}
_PR2 = {**_PR, "number": 2, "title": "second", "html_url": "u2"}
_ISSUE = {
    "number": 77,
    "title": "an issue",
    "state": "open",
    "body": "body",
    "user": {"login": "carol"},
    "comments": 2,
    "html_url": "u77",
    "created_at": "2026-01-03T00:00:00Z",
    "updated_at": "2026-01-03T00:00:00Z",
}


def _github_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/pulls"):
        _gh_calls["pulls"] += 1
        if _gh_calls["pulls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[_PR2])
        link = '<https://api.github.com/repos/o/r/pulls?page=2&per_page=100>; rel="next"'
        return httpx.Response(200, json=[_PR], headers={"Link": link})
    if path.endswith("/issues"):
        return httpx.Response(200, json=[_ISSUE])
    if path.endswith("/issues/77/comments"):
        return httpx.Response(
            200,
            json=[
                {"user": {"login": "dan"}, "body": "first comment"},
                {"user": {"login": "eve"}, "body": "second comment"},
            ],
        )
    return httpx.Response(404, json={})


def _slack_handler(request: httpx.Request) -> httpx.Response:
    path, cur = request.url.path, request.url.params.get("cursor")
    if path.endswith("/users.list"):
        return httpx.Response(
            200,
            json={
                "ok": True,
                "members": [
                    {"id": "U1", "profile": {"display_name": "alice"}},
                    {"id": "U2", "profile": {"display_name": "bob"}},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )
    if path.endswith("/conversations.history"):
        if not cur:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {
                            "type": "message",
                            "user": "U1",
                            "ts": "100.1",
                            "thread_ts": "100.1",
                            "reply_count": 1,
                            "text": "root",
                        }
                    ],
                    "response_metadata": {"next_cursor": "PAGE2"},
                },
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"type": "message", "user": "U2", "ts": "200.2", "text": "standalone"}
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )
    if path.endswith("/conversations.replies"):
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"user": "U1", "ts": "100.1", "text": "root"},
                    {"user": "U2", "ts": "100.2", "text": "a reply"},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )
    return httpx.Response(200, json={"ok": False, "error": "unknown_method"})


def test_github_pagination_backoff_and_comments(db):
    _gh_calls["pulls"] = 0
    db.execute(text("TRUNCATE raw_documents, ingestion_runs RESTART IDENTITY CASCADE"))
    db.commit()
    GitHubConnector(db, repo="o/r", token="x", transport=httpx.MockTransport(_github_handler)).run()

    prs = db.scalars(select(RawDocument).where(RawDocument.source_type == "pull_request")).all()
    assert len(prs) == 2  # Link-header pagination
    assert _gh_calls["pulls"] >= 3  # 429, then page 1, then page 2 (backoff recovered)
    issue = db.scalar(select(RawDocument).where(RawDocument.source_type == "issue"))
    assert "first comment" in issue.content and "second comment" in issue.content


def test_github_incremental_cursor(db):
    db.execute(text("TRUNCATE raw_documents, ingestion_runs RESTART IDENTITY CASCADE"))
    db.commit()
    seen = {"since": None, "pulls_params": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls"):
            seen["pulls_params"] = dict(request.url.params)
            return httpx.Response(200, json=[_PR])
        if path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if path.endswith("/issues"):
            seen["since"] = request.url.params.get("since")
            return httpx.Response(200, json=[_ISSUE])
        return httpx.Response(404, json={})

    def connector():
        return GitHubConnector(db, repo="o/r", token="x", transport=httpx.MockTransport(handler))

    run1, docs1 = connector().run()
    assert seen["since"] is None
    assert run1.cursor == "2026-01-03T00:00:00Z"  # newest updated_at (the issue)
    assert len(docs1) == 2

    run2, docs2 = connector().run()
    assert seen["since"] == run1.cursor  # issues filtered server-side
    assert seen["pulls_params"]["sort"] == "updated"
    # The PR (updated before the cursor) is dropped by the stop predicate.
    assert [d.source_type for d in docs2] == ["issue"]
    assert run2.cursor == run1.cursor


def test_slack_incremental_cursor(db):
    db.execute(text("TRUNCATE raw_documents, ingestion_runs RESTART IDENTITY CASCADE"))
    db.commit()
    seen = {"oldest": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        meta = {"response_metadata": {"next_cursor": ""}}
        if path.endswith("/users.list"):
            return httpx.Response(200, json={"ok": True, "members": [], **meta})
        if path.endswith("/conversations.history"):
            seen["oldest"] = request.url.params.get("oldest")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {"type": "message", "user": "U1", "ts": "200.2", "text": "newer"},
                        {"type": "message", "user": "U2", "ts": "100.1", "text": "older"},
                    ],
                    **meta,
                },
            )
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})

    def connector():
        return SlackConnector(
            db, channel_id="C1", token="x", transport=httpx.MockTransport(handler)
        )

    run1, _ = connector().run()
    assert seen["oldest"] is None
    assert run1.cursor == "200.2"

    connector().run()
    assert seen["oldest"] == "200.2"


def test_slack_cursor_pagination_and_user_resolution(db):
    db.execute(text("TRUNCATE raw_documents, ingestion_runs RESTART IDENTITY CASCADE"))
    db.commit()
    SlackConnector(
        db, channel_id="C1", token="x", transport=httpx.MockTransport(_slack_handler)
    ).run()

    threads = db.scalars(select(RawDocument).where(RawDocument.source_type == "thread")).all()
    assert len(threads) == 2  # two pages of history
    threaded = db.scalar(select(RawDocument).where(RawDocument.external_id == "100.1"))
    assert "alice:" in threaded.content and "bob:" in threaded.content  # ids resolved


_JIRA_ISSUE = {
    "key": "OPS-1",
    "fields": {
        "summary": "Checkout deploy failure",
        "description": "Pods crash-loop after the checkout-v2 rollout.",
        "status": {"name": "In Progress"},
        "creator": {"displayName": "Alice"},
        "created": "2026-05-20T14:00:00.000+0000",
        "updated": "2026-05-20T15:00:00.000+0000",
        "comment": {
            "comments": [
                {"author": {"displayName": "Bob"}, "body": "Rolled back to v1."},
            ]
        },
    },
}
_JIRA_ISSUE_2 = {
    "key": "OPS-2",
    "fields": {
        "summary": "Follow-up hardening",
        "description": None,
        "status": {"name": "To Do"},
        "creator": {"displayName": "Bob"},
        "created": "2026-05-21T09:00:00.000+0000",
        "updated": "2026-05-21T10:30:00.000+0000",
        "comment": {"comments": []},
    },
}


def test_jira_pagination_comments_and_cursor(db):
    db.execute(text("TRUNCATE raw_documents, ingestion_runs RESTART IDENTITY CASCADE"))
    db.commit()
    seen = {"jql": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/2/search"
        seen["jql"].append(request.url.params.get("jql"))
        start_at = int(request.url.params.get("startAt", 0))
        page = [_JIRA_ISSUE] if start_at == 0 else [_JIRA_ISSUE_2]
        return httpx.Response(200, json={"issues": page, "total": 2, "startAt": start_at})

    def connector():
        return JiraConnector(
            db,
            project="OPS",
            base_url="https://acme.atlassian.net",
            email="e@x.com",
            api_token="t",
            transport=httpx.MockTransport(handler),
        )

    run1, docs1 = connector().run()
    assert len(docs1) == 2
    assert run1.cursor == "2026-05-21T10:30:00Z"
    issue = db.scalar(select(RawDocument).where(RawDocument.external_id == "OPS-1"))
    assert issue.source_system == "jira"
    assert "Rolled back to v1." in issue.content
    assert issue.url == "https://acme.atlassian.net/browse/OPS-1"
    assert 'project = "OPS" ORDER BY updated ASC' in seen["jql"][0]

    connector().run()
    assert 'updated >= "2026-05-21 10:30"' in seen["jql"][-1]


def test_jira_requires_configuration(db):
    try:
        JiraConnector(db, project="OPS", base_url=None, email="e@x.com", api_token="t")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "JIRA_BASE_URL" in str(exc)
