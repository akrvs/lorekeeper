"""GitHub connector — live, async GitHub REST API ingestion.

Pulls a repository's Pull Requests (title, body, state) and Issues (body +
comments) into `raw_documents`. Network I/O is async (httpx.AsyncClient) with
Link-header pagination and the shared retry/backoff layer; the sync `fetch()`
required by BaseConnector bridges to it via asyncio.run so the rest of the
pipeline (sync DB + extraction) is untouched.

Syncs are incremental: the newest `updated_at` seen is persisted as the run
cursor, later runs pass it as `since=` on issues and stop paginating PRs
(sorted by updated, descending) once they reach it.

Auth: a Personal Access Token (GITHUB_TOKEN). For private repos / higher rate
limits use a fine-grained PAT with `Contents: read`, `Pull requests: read`,
`Issues: read`.
"""

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors._http import build_async_client, request_with_retries
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory

logger = logging.getLogger("company_brain.connectors.github")

_API_VERSION = "2022-11-28"


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@ConnectorFactory.register("github")
class GitHubConnector(BaseConnector):
    source_system = "github"

    def __init__(
        self,
        db: Session,
        *,
        repo: str | None = None,
        token: str | None = None,
        max_items: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_,
    ):
        super().__init__(db)
        self.repo = repo or settings.github_repo
        self.token = token or settings.github_token
        if not self.repo or "/" not in self.repo:
            raise ValueError("GitHubConnector requires repo as 'owner/name'.")
        if not self.token:
            raise ValueError("GitHubConnector requires a token (GITHUB_TOKEN).")
        self.resource_key = self.repo
        self.max_items = max_items or settings.ingest_max_items
        self._transport = transport
        self._since: str | None = None

    # --- BaseConnector contract (sync bridge to the async implementation) ---
    def fetch(self) -> Iterable[RawDoc]:
        self._since = self.last_cursor()
        return run_blocking(self._fetch_all())

    # --- async implementation ----------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        return build_async_client(
            settings.github_api_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": "company-brain",
            },
            transport=self._transport,
        )

    async def _fetch_all(self) -> list[RawDoc]:
        async with self._client() as client:
            pulls, issues = await asyncio.gather(
                self._fetch_pull_requests(client),
                self._fetch_issues(client),
            )
        docs = pulls + issues
        stamps = [d.source_updated_at for d in docs if d.source_updated_at]
        if stamps:
            self.new_cursor = max(stamps).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            self.new_cursor = self._since
        logger.info("github %s: %d PRs, %d issues", self.repo, len(pulls), len(issues))
        return docs

    def _older_than_cursor(self, item: dict) -> bool:
        ts = item.get("updated_at")
        return bool(self._since and ts and ts < self._since)

    async def _paginate(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict,
        stop: Callable[[dict], bool] | None = None,
    ) -> list[dict]:
        """Collect list results across pages, following the Link rel=next header.
        `stop` drops matching items and ends pagination at the first match."""
        results: list[dict] = []
        url: str | None = path
        query: dict | None = {**params, "per_page": 100}
        while url and len(results) < self.max_items:
            resp = await request_with_retries(client, "GET", url, params=query)
            page = resp.json()
            if not isinstance(page, list):
                break
            if stop is not None:
                fresh = [item for item in page if not stop(item)]
                results.extend(fresh)
                if len(fresh) < len(page):
                    break
            else:
                results.extend(page)
            url = _next_link(resp)  # absolute URL or None
            query = None  # the next URL already carries the params
        return results[: self.max_items]

    async def _fetch_pull_requests(self, client: httpx.AsyncClient) -> list[RawDoc]:
        items = await self._paginate(
            client,
            f"/repos/{self.repo}/pulls",
            {"state": "all", "sort": "updated", "direction": "desc"},
            stop=self._older_than_cursor,
        )
        docs: list[RawDoc] = []
        for pr in items:
            state = "merged" if pr.get("merged_at") else pr.get("state", "unknown")
            body = pr.get("body") or ""
            docs.append(
                RawDoc(
                    source_type="pull_request",
                    external_id=str(pr["number"]),
                    resource_key=self.repo,
                    title=pr.get("title"),
                    url=pr.get("html_url"),
                    author=(pr.get("user") or {}).get("login"),
                    content=f"PR #{pr['number']}: {pr.get('title', '')}\nState: {state}\n\n{body}",
                    raw_payload=pr,
                    source_created_at=_iso(pr.get("created_at")),
                    source_updated_at=_iso(pr.get("updated_at")),
                )
            )
        return docs

    async def _fetch_issues(self, client: httpx.AsyncClient) -> list[RawDoc]:
        params = {"state": "all"}
        if self._since:
            params["since"] = self._since
        items = await self._paginate(client, f"/repos/{self.repo}/issues", params)
        # GitHub's issues endpoint also returns PRs — drop them (we ingest PRs separately).
        issues = [i for i in items if "pull_request" not in i]

        async def build(issue: dict) -> RawDoc:
            comments: list[dict] = []
            if issue.get("comments", 0):
                comments = await self._paginate(
                    client, f"/repos/{self.repo}/issues/{issue['number']}/comments", {}
                )
            body = issue.get("body") or ""
            header = f"Issue #{issue['number']}: {issue.get('title', '')}"
            content = f"{header}\nState: {issue.get('state')}\n\n{body}"
            if comments:
                transcript = "\n\n".join(
                    f"comment by {(c.get('user') or {}).get('login', 'unknown')}: "
                    f"{c.get('body', '')}"
                    for c in comments
                )
                content += f"\n\n--- Comments ({len(comments)}) ---\n{transcript}"
            return RawDoc(
                source_type="issue",
                external_id=str(issue["number"]),
                resource_key=self.repo,
                title=issue.get("title"),
                url=issue.get("html_url"),
                author=(issue.get("user") or {}).get("login"),
                content=content,
                raw_payload={"issue": issue, "comments": comments},
                source_created_at=_iso(issue.get("created_at")),
                source_updated_at=_iso(issue.get("updated_at")),
            )

        return list(await asyncio.gather(*(build(i) for i in issues)))


def _next_link(resp: httpx.Response) -> str | None:
    """Parse the GitHub `Link` header for the rel="next" URL."""
    link = resp.headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().strip("<>")
        if any(s.strip() == 'rel="next"' for s in segments[1:]):
            return url
    return None
