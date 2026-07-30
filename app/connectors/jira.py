import base64
import logging
from collections.abc import Iterable
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors._http import build_async_client, request_with_retries
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory

logger = logging.getLogger("company_brain.connectors.jira")

_FIELDS = "summary,description,status,creator,created,updated,comment"


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


@ConnectorFactory.register("jira")
class JiraConnector(BaseConnector):
    source_system = "jira"

    def __init__(
        self,
        db: Session,
        *,
        project: str | None = None,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        max_items: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_,
    ):
        super().__init__(db)
        self.base_url = base_url or settings.jira_base_url
        self.email = email or settings.jira_email
        self.api_token = api_token or settings.jira_api_token
        self.project = project or settings.jira_project
        if not self.base_url:
            raise ValueError("JiraConnector requires a site URL (JIRA_BASE_URL).")
        if not self.email or not self.api_token:
            raise ValueError("JiraConnector requires credentials (JIRA_EMAIL, JIRA_API_TOKEN).")
        if not self.project:
            raise ValueError("JiraConnector requires a project key (JIRA_PROJECT).")
        self.resource_key = self.project
        self.max_items = max_items or settings.ingest_max_items
        self._transport = transport
        self._since: str | None = None

    def fetch(self) -> Iterable[RawDoc]:
        self._since = self.last_cursor()
        return run_blocking(self._fetch_all())

    def _client(self) -> httpx.AsyncClient:
        credentials = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
        return build_async_client(
            self.base_url,
            headers={
                "Authorization": f"Basic {credentials}",
                "Accept": "application/json",
                "User-Agent": "company-brain",
            },
            transport=self._transport,
        )

    def _jql(self) -> str:
        jql = f'project = "{self.project}"'
        since = _ts(self._since)
        if since is not None:
            jql += f' AND updated >= "{since.strftime("%Y-%m-%d %H:%M")}"'
        return jql + " ORDER BY updated ASC"

    async def _fetch_all(self) -> list[RawDoc]:
        issues: list[dict] = []
        async with self._client() as client:
            start_at = 0
            while len(issues) < self.max_items:
                resp = await request_with_retries(
                    client,
                    "GET",
                    "/rest/api/2/search",
                    params={
                        "jql": self._jql(),
                        "startAt": start_at,
                        "maxResults": 100,
                        "fields": _FIELDS,
                    },
                )
                payload = resp.json()
                page = payload.get("issues", [])
                if not page:
                    break
                issues.extend(page)
                start_at += len(page)
                if start_at >= payload.get("total", 0):
                    break
        issues = issues[: self.max_items]

        docs = [self._build_doc(issue) for issue in issues]
        stamps = [d.source_updated_at for d in docs if d.source_updated_at]
        if stamps:
            self.new_cursor = max(stamps).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            self.new_cursor = self._since
        logger.info("jira %s: %d issues", self.project, len(docs))
        return docs

    def _build_doc(self, issue: dict) -> RawDoc:
        key = issue["key"]
        fields = issue.get("fields") or {}
        status = ((fields.get("status") or {}).get("name")) or "unknown"
        summary = fields.get("summary") or ""
        content = f"Jira {key}: {summary}\nStatus: {status}\n\n{fields.get('description') or ''}"
        comments = ((fields.get("comment") or {}).get("comments")) or []
        if comments:
            transcript = "\n\n".join(
                f"comment by {(c.get('author') or {}).get('displayName', 'unknown')}: "
                f"{c.get('body', '')}"
                for c in comments
            )
            content += f"\n\n--- Comments ({len(comments)}) ---\n{transcript}"
        return RawDoc(
            source_type="jira_issue",
            external_id=key,
            resource_key=self.project,
            title=summary or key,
            url=f"{self.base_url.rstrip('/')}/browse/{key}",
            author=(fields.get("creator") or {}).get("displayName"),
            content=content,
            raw_payload=issue,
            source_created_at=_ts(fields.get("created")),
            source_updated_at=_ts(fields.get("updated")),
        )
