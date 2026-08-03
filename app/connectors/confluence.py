import logging
from collections.abc import Iterable

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors._http import build_async_client, request_with_retries
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory
from app.connectors.jira import _ts
from app.textutil import strip_html

logger = logging.getLogger("company_brain.connectors.confluence")

_EXPAND = "body.storage,version,space,history"


@ConnectorFactory.register("confluence")
class ConfluenceConnector(BaseConnector):
    source_system = "confluence"

    def __init__(
        self,
        db: Session,
        *,
        space: str | None = None,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        max_items: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_,
    ):
        super().__init__(db)
        self.base_url = (base_url or settings.confluence_base_url or "").rstrip("/")
        self.email = email or settings.confluence_email
        self.api_token = api_token or settings.confluence_api_token
        if not self.base_url:
            raise ValueError("ConfluenceConnector requires a site URL (CONFLUENCE_BASE_URL).")
        if not self.email or not self.api_token:
            raise ValueError(
                "ConfluenceConnector requires credentials (CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN)."
            )
        self.space = space or settings.confluence_space
        self.resource_key = self.space or "all"
        self.max_items = max_items or settings.ingest_max_items
        self._transport = transport
        self._since: str | None = None

    def fetch(self) -> Iterable[RawDoc]:
        self._since = self.last_cursor()
        return run_blocking(self._fetch_all())

    def _client(self) -> httpx.AsyncClient:
        import base64

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

    def _cql(self) -> str:
        parts = ['type = "page"']
        if self.space:
            parts.append(f'space = "{self.space}"')
        since = _ts(self._since)
        if since is not None:
            parts.append(f'lastmodified >= "{since.strftime("%Y/%m/%d %H:%M")}"')
        return " AND ".join(parts) + " ORDER BY lastmodified ASC"

    async def _fetch_all(self) -> list[RawDoc]:
        docs: list[RawDoc] = []
        async with self._client() as client:
            start = 0
            while len(docs) < self.max_items:
                resp = await request_with_retries(
                    client,
                    "GET",
                    "/wiki/rest/api/content/search",
                    params={"cql": self._cql(), "expand": _EXPAND, "start": start, "limit": 50},
                )
                results = resp.json().get("results", [])
                if not results:
                    break
                for page in results:
                    comments = await self._comments(client, page["id"])
                    docs.append(self._build_doc(page, comments))
                start += len(results)
                if len(results) < 50:
                    break
        docs = docs[: self.max_items]

        stamps = [d.source_updated_at for d in docs if d.source_updated_at]
        if stamps:
            self.new_cursor = max(stamps).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            self.new_cursor = self._since
        logger.info("confluence %s: %d pages", self.resource_key, len(docs))
        return docs

    async def _comments(self, client: httpx.AsyncClient, page_id: str) -> list[dict]:
        # ponytail: first 50 comments per page, unpaginated; paginate if a
        # page ever carries more.
        resp = await request_with_retries(
            client,
            "GET",
            f"/wiki/rest/api/content/{page_id}/child/comment",
            params={"expand": "body.storage,history", "limit": 50},
        )
        return resp.json().get("results", [])

    @staticmethod
    def _comment_author(comment: dict) -> str:
        created_by = (comment.get("history") or {}).get("createdBy") or {}
        return created_by.get("displayName", "unknown")

    def _build_doc(self, page: dict, comments: list[dict]) -> RawDoc:
        title = page.get("title") or ""
        body = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
        content = f"Confluence page: {title}\n\n{strip_html(body)}"
        if comments:
            transcript = "\n\n".join(
                f"comment by {self._comment_author(c)}: "
                f"{strip_html(((c.get('body') or {}).get('storage') or {}).get('value'))}"
                for c in comments
            )
            content += f"\n\n--- Comments ({len(comments)}) ---\n{transcript}"
        webui = (page.get("_links") or {}).get("webui")
        history = page.get("history") or {}
        return RawDoc(
            source_type="confluence_page",
            external_id=str(page["id"]),
            resource_key=((page.get("space") or {}).get("key")) or self.resource_key,
            title=title or str(page["id"]),
            url=f"{self.base_url}/wiki{webui}" if webui else None,
            author=((history.get("createdBy")) or {}).get("displayName"),
            content=content,
            raw_payload=page,
            source_created_at=_ts(history.get("createdDate")),
            source_updated_at=_ts((page.get("version") or {}).get("when")),
        )
