"""Notion connector (Track 4) — async, with recursive block/sub-page traversal.

Queries a Notion database, then for each page walks its block tree (depth-first,
following child pages) and renders a plain-text transcript. Uses the shared
retry/backoff layer (Notion returns 429 + Retry-After under load).

Auth: an internal integration token (NOTION_TOKEN) shared with the database.
"""

import asyncio
from collections.abc import Iterable

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors._http import build_async_client, request_with_retries
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory

_MAX_BLOCK_DEPTH = 4


def _plain_text(rich: list[dict] | None) -> str:
    return "".join(rt.get("plain_text", "") for rt in (rich or []))


def _page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _plain_text(prop.get("title")) or "(untitled)"
    return "(untitled)"


@ConnectorFactory.register("notion")
class NotionConnector(BaseConnector):
    source_system = "notion"

    def __init__(
        self,
        db: Session,
        *,
        database_id: str | None = None,
        token: str | None = None,
        max_items: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_,
    ):
        super().__init__(db)
        self.database_id = database_id or settings.notion_database_id
        self.token = token or settings.notion_token
        if not self.database_id:
            raise ValueError("NotionConnector requires a database_id (NOTION_DATABASE_ID).")
        if not self.token:
            raise ValueError("NotionConnector requires a token (NOTION_TOKEN).")
        self.max_items = max_items or settings.ingest_max_items
        self._transport = transport

    def fetch(self) -> Iterable[RawDoc]:
        return run_blocking(self._fetch_all())

    def _client(self) -> httpx.AsyncClient:
        return build_async_client(
            settings.notion_api_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": settings.notion_version,
            },
            transport=self._transport,
        )

    async def _fetch_all(self) -> list[RawDoc]:
        async with self._client() as client:
            pages = await self._query_database(client)
            return list(await asyncio.gather(*(self._page_doc(client, p) for p in pages)))

    async def _query_database(self, client: httpx.AsyncClient) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        while len(results) < self.max_items:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            resp = await request_with_retries(
                client, "POST", f"/databases/{self.database_id}/query", json=body
            )
            data = resp.json()
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results[: self.max_items]

    async def _blocks_text(self, client: httpx.AsyncClient, block_id: str, depth: int) -> str:
        if depth > _MAX_BLOCK_DEPTH:
            return ""
        lines: list[str] = []
        cursor: str | None = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = await request_with_retries(
                client, "GET", f"/blocks/{block_id}/children", params=params
            )
            data = resp.json()
            for block in data.get("results", []):
                btype = block.get("type", "")
                text = _plain_text((block.get(btype) or {}).get("rich_text"))
                if text:
                    lines.append(text)
                if block.get("has_children"):  # nested blocks / sub-pages
                    lines.append(await self._blocks_text(client, block["id"], depth + 1))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return "\n".join(filter(None, lines))

    async def _page_doc(self, client: httpx.AsyncClient, page: dict) -> RawDoc:
        title = _page_title(page)
        body = await self._blocks_text(client, page["id"], depth=0)
        return RawDoc(
            source_type="page",
            external_id=page["id"],
            resource_key=self.database_id,
            title=title,
            url=page.get("url"),
            content=f"{title}\n\n{body}",
            raw_payload=page,
            source_created_at=None,
            source_updated_at=None,
        )
