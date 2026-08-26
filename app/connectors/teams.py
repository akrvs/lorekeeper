"""Microsoft Teams connector (Track 4) — registered scaffold, MS Graph.

Structurally complete and ready for credentials: OAuth2 client-credentials token
exchange, then channel → message → reply traversal assembled into thread
transcripts (same shape as Slack). Graph throttling (429 + Retry-After) is handled
by the shared retry layer. Wire-up point: a tenant app registration with
`ChannelMessage.Read.All` application permission.
"""

import logging
from collections.abc import Iterable

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors._http import assert_upstream_url, build_async_client, request_with_retries
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory

logger = logging.getLogger("company_brain.connectors.teams")


@ConnectorFactory.register("teams")
class TeamsConnector(BaseConnector):
    source_system = "teams"

    def __init__(
        self,
        db: Session,
        *,
        team_id: str | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        max_items: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_,
    ):
        super().__init__(db)
        self.team_id = team_id or settings.teams_team_id
        self.tenant_id = tenant_id or settings.teams_tenant_id
        self.client_id = client_id or settings.teams_client_id
        self.client_secret = client_secret or settings.teams_client_secret
        if not all((self.team_id, self.tenant_id, self.client_id, self.client_secret)):
            raise ValueError(
                "TeamsConnector requires TEAMS_TEAM_ID/TENANT_ID/CLIENT_ID/CLIENT_SECRET."
            )
        self.resource_key = self.team_id
        self.max_items = max_items or settings.ingest_max_items
        self._transport = transport

    def fetch(self) -> Iterable[RawDoc]:
        return run_blocking(self._fetch_all())

    async def _access_token(self) -> str:
        url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        async with build_async_client("", {}, transport=self._transport) as client:
            resp = await request_with_retries(
                client,
                "POST",
                url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
            )
            return resp.json()["access_token"]

    def _client(self, token: str) -> httpx.AsyncClient:
        return build_async_client(
            settings.graph_api_url,
            headers={"Authorization": f"Bearer {token}"},
            transport=self._transport,
        )

    async def _paginate(self, client: httpx.AsyncClient, path: str) -> list[dict]:
        results: list[dict] = []
        url: str | None = path
        while url and len(results) < self.max_items:
            resp = await request_with_retries(client, "GET", url)
            data = resp.json()
            results.extend(data.get("value", []))
            next_link = data.get("@odata.nextLink")
            url = assert_upstream_url(next_link, str(client.base_url)) if next_link else None
        return results[: self.max_items]

    async def _fetch_all(self) -> list[RawDoc]:
        token = await self._access_token()
        async with self._client(token) as client:
            docs: list[RawDoc] = []
            channels = await self._paginate(client, f"/teams/{self.team_id}/channels")
            for ch in channels:
                base = f"/teams/{self.team_id}/channels/{ch['id']}/messages"
                for msg in await self._paginate(client, base):
                    replies = await self._paginate(client, f"{base}/{msg['id']}/replies")
                    docs.append(self._thread_doc(ch, msg, replies))
            logger.info("teams %s: %d threads", self.team_id, len(docs))
            return docs

    def _thread_doc(self, channel: dict, root: dict, replies: list[dict]) -> RawDoc:
        def body(m: dict) -> str:
            who = ((m.get("from") or {}).get("user") or {}).get("displayName", "unknown")
            return f"{who}: {(m.get('body') or {}).get('content', '')}"

        transcript = "\n".join(body(m) for m in [root, *replies])
        return RawDoc(
            source_type="thread",
            external_id=root["id"],
            resource_key=f"{self.team_id}/{channel['id']}",
            title=f"Teams thread in {channel.get('displayName', channel['id'])}",
            url=root.get("webUrl"),
            content=transcript,
            raw_payload={"channel": channel, "root": root, "replies": replies},
        )
