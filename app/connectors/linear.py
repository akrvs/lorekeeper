import logging
from collections.abc import Iterable
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors._http import build_async_client, request_with_retries
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory

logger = logging.getLogger("company_brain.connectors.linear")

_QUERY = """
query Issues($after: String, $filter: IssueFilter) {
  issues(first: 50, after: $after, filter: $filter, orderBy: updatedAt) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier
      title
      description
      url
      createdAt
      updatedAt
      state { name }
      creator { displayName }
      comments { nodes { body user { displayName } } }
    }
  }
}
"""


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@ConnectorFactory.register("linear")
class LinearConnector(BaseConnector):
    source_system = "linear"

    def __init__(
        self,
        db: Session,
        *,
        api_key: str | None = None,
        team: str | None = None,
        max_items: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_,
    ):
        super().__init__(db)
        self.api_key = api_key or settings.linear_api_key
        if not self.api_key:
            raise ValueError("LinearConnector requires an API key (LINEAR_API_KEY).")
        self.team = team or settings.linear_team
        self.resource_key = self.team or "all"
        self.max_items = max_items or settings.ingest_max_items
        self._transport = transport
        self._since: str | None = None

    def fetch(self) -> Iterable[RawDoc]:
        self._since = self.last_cursor()
        return run_blocking(self._fetch_all())

    def _client(self) -> httpx.AsyncClient:
        # Linear personal API keys are sent bare in the Authorization header.
        return build_async_client(
            settings.linear_api_url,
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "company-brain",
            },
            transport=self._transport,
        )

    def _filter(self) -> dict:
        issue_filter: dict = {}
        if self.team:
            issue_filter["team"] = {"key": {"eq": self.team}}
        if self._since:
            issue_filter["updatedAt"] = {"gt": self._since}
        return issue_filter

    async def _fetch_all(self) -> list[RawDoc]:
        issues: list[dict] = []
        async with self._client() as client:
            after: str | None = None
            while len(issues) < self.max_items:
                resp = await request_with_retries(
                    client,
                    "POST",
                    "/graphql",
                    json={"query": _QUERY, "variables": {"after": after, "filter": self._filter()}},
                )
                payload = resp.json()
                if payload.get("errors"):
                    raise ValueError(f"Linear GraphQL error: {payload['errors']}")
                connection = payload["data"]["issues"]
                issues.extend(connection["nodes"])
                if not connection["pageInfo"]["hasNextPage"]:
                    break
                after = connection["pageInfo"]["endCursor"]
        issues = issues[: self.max_items]

        docs = [self._build_doc(issue) for issue in issues]
        stamps = [d.source_updated_at for d in docs if d.source_updated_at]
        if stamps:
            self.new_cursor = max(stamps).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        else:
            self.new_cursor = self._since
        logger.info("linear %s: %d issues", self.resource_key, len(docs))
        return docs

    def _build_doc(self, issue: dict) -> RawDoc:
        identifier = issue["identifier"]
        state = ((issue.get("state") or {}).get("name")) or "unknown"
        title = issue.get("title") or ""
        content = (
            f"Linear {identifier}: {title}\nStatus: {state}\n\n{issue.get('description') or ''}"
        )
        comments = ((issue.get("comments") or {}).get("nodes")) or []
        if comments:
            transcript = "\n\n".join(
                f"comment by {(c.get('user') or {}).get('displayName', 'unknown')}: "
                f"{c.get('body', '')}"
                for c in comments
            )
            content += f"\n\n--- Comments ({len(comments)}) ---\n{transcript}"
        return RawDoc(
            source_type="linear_issue",
            external_id=identifier,
            resource_key=self.resource_key,
            title=title or identifier,
            url=issue.get("url"),
            author=(issue.get("creator") or {}).get("displayName"),
            content=content,
            raw_payload=issue,
            source_created_at=_ts(issue.get("createdAt")),
            source_updated_at=_ts(issue.get("updatedAt")),
        )
