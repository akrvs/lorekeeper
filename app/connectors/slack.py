"""Slack connector — live, async Slack Web API ingestion.

Reads a channel's history (`conversations.history`) and, for any message with
replies, the full thread (`conversations.replies`). Each thread becomes one
`raw_documents` row (a readable transcript). User IDs are resolved to display
names via a best-effort `users.list` call (gracefully degrades to raw IDs if
the token lacks the `users:read` scope).

Auth: a Bot User OAuth Token (SLACK_BOT_TOKEN) with scopes:
`channels:history` (or `groups:history`), `channels:read`, `users:read`.
"""

import logging
from collections.abc import Iterable
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors._http import ConnectorError, build_async_client, request_with_retries
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory

logger = logging.getLogger("company_brain.connectors.slack")


def _ts_to_dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC)
    except (ValueError, OSError):
        return None


@ConnectorFactory.register("slack")
class SlackConnector(BaseConnector):
    source_system = "slack"

    def __init__(
        self,
        db: Session,
        *,
        channel_id: str | None = None,
        token: str | None = None,
        max_items: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_,
    ):
        super().__init__(db)
        self.channel_id = channel_id or settings.slack_channel_id
        self.token = token or settings.slack_bot_token
        if not self.channel_id:
            raise ValueError("SlackConnector requires a channel_id (SLACK_CHANNEL_ID).")
        if not self.token:
            raise ValueError("SlackConnector requires a token (SLACK_BOT_TOKEN).")
        self.max_items = max_items or settings.ingest_max_items
        self._transport = transport

    # --- BaseConnector contract (sync bridge to async) ----------------------
    def fetch(self) -> Iterable[RawDoc]:
        return run_blocking(self._fetch_all())

    # --- async implementation ----------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        return build_async_client(
            settings.slack_api_url,
            headers={"Authorization": f"Bearer {self.token}", "User-Agent": "company-brain"},
            transport=self._transport,
        )

    async def _fetch_all(self) -> list[RawDoc]:
        async with self._client() as client:
            users = await self._load_user_map(client)
            history = await self._cursor_paginate(
                client,
                "/conversations.history",
                {"channel": self.channel_id, "limit": 200},
                "messages",
            )

            docs: list[RawDoc] = []
            for msg in history:
                if msg.get("subtype"):  # skip joins/leaves/bot system messages
                    continue
                root_ts = msg.get("thread_ts") or msg.get("ts")
                if not root_ts:
                    continue
                if msg.get("reply_count"):
                    messages = await self._cursor_paginate(
                        client,
                        "/conversations.replies",
                        {"channel": self.channel_id, "ts": root_ts, "limit": 200},
                        "messages",
                    )
                else:
                    messages = [msg]
                docs.append(self._thread_doc(root_ts, messages, users))

        logger.info("slack %s: %d threads", self.channel_id, len(docs))
        return docs

    async def _cursor_paginate(
        self, client: httpx.AsyncClient, path: str, params: dict, key: str
    ) -> list[dict]:
        """Page a Slack endpoint via response_metadata.next_cursor."""
        results: list[dict] = []
        cursor: str | None = None
        while len(results) < self.max_items:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            resp = await request_with_retries(client, "GET", path, params=page_params)
            data = resp.json()
            if not data.get("ok"):
                # Slack signals logical errors with HTTP 200 + ok:false.
                raise ConnectorError(f"Slack API error on {path}: {data.get('error', 'unknown')}")
            results.extend(data.get(key, []))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return results[: self.max_items]

    async def _load_user_map(self, client: httpx.AsyncClient) -> dict[str, str]:
        """Best-effort id -> display name. Degrades to {} on missing scope."""
        try:
            members = await self._cursor_paginate(client, "/users.list", {"limit": 200}, "members")
        except ConnectorError as exc:
            logger.warning("users.list unavailable (%s); using raw user ids", exc)
            return {}
        mapping: dict[str, str] = {}
        for m in members:
            profile = m.get("profile") or {}
            mapping[m["id"]] = (
                profile.get("display_name") or m.get("real_name") or m.get("name") or m["id"]
            )
        return mapping

    def _thread_doc(self, root_ts: str, messages: list[dict], users: dict[str, str]) -> RawDoc:
        def name_of(m: dict) -> str:
            uid = m.get("user") or m.get("bot_id") or "unknown"
            return users.get(uid, uid)

        transcript = "\n".join(f"{name_of(m)}: {m.get('text', '')}" for m in messages)
        permalink = f"https://app.slack.com/archives/{self.channel_id}/p{root_ts.replace('.', '')}"
        return RawDoc(
            source_type="thread",
            external_id=root_ts,
            resource_key=self.channel_id,
            title=f"Slack thread {root_ts} in {self.channel_id} ({len(messages)} msgs)",
            url=permalink,
            author=name_of(messages[0]) if messages else None,
            content=transcript,
            raw_payload={"channel": self.channel_id, "thread_ts": root_ts, "messages": messages},
            source_created_at=_ts_to_dt(messages[0].get("ts")) if messages else None,
            source_updated_at=_ts_to_dt(messages[-1].get("ts")) if messages else None,
        )
