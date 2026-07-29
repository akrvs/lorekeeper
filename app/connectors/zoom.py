"""Zoom connector (Track 4) — registered scaffold, transcript-driven.

Server-to-Server OAuth, then list cloud recordings and pull each meeting's
TRANSCRIPT file (WEBVTT), parsed by the shared transcript engine into speaker
turns. Wire-up point: a Server-to-Server OAuth app with `cloud_recording:read`.
"""

import base64
import logging
from collections.abc import Iterable

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors._http import build_async_client, request_with_retries
from app.connectors._transcript import parse_vtt, transcript_to_text
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory

logger = logging.getLogger("company_brain.connectors.zoom")


@ConnectorFactory.register("zoom")
class ZoomConnector(BaseConnector):
    source_system = "zoom"

    def __init__(
        self,
        db: Session,
        *,
        account_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        max_items: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_,
    ):
        super().__init__(db)
        self.account_id = account_id or settings.zoom_account_id
        self.client_id = client_id or settings.zoom_client_id
        self.client_secret = client_secret or settings.zoom_client_secret
        if not all((self.account_id, self.client_id, self.client_secret)):
            raise ValueError("ZoomConnector requires ZOOM_ACCOUNT_ID/CLIENT_ID/CLIENT_SECRET.")
        self.resource_key = self.account_id
        self.max_items = max_items or settings.ingest_max_items
        self._transport = transport

    def fetch(self) -> Iterable[RawDoc]:
        return run_blocking(self._fetch_all())

    async def _access_token(self) -> str:
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        headers = {"Authorization": f"Basic {basic}"}
        async with build_async_client("", headers, transport=self._transport) as client:
            resp = await request_with_retries(
                client,
                "POST",
                "https://zoom.us/oauth/token",
                params={"grant_type": "account_credentials", "account_id": self.account_id},
            )
            return resp.json()["access_token"]

    async def _fetch_all(self) -> list[RawDoc]:
        token = await self._access_token()
        client = build_async_client(
            settings.zoom_api_url,
            headers={"Authorization": f"Bearer {token}"},
            transport=self._transport,
        )
        async with client:
            resp = await request_with_retries(client, "GET", "/users/me/recordings")
            meetings = resp.json().get("meetings", [])[: self.max_items]
            docs: list[RawDoc] = []
            for mt in meetings:
                vtt = await self._download_transcript(client, mt)
                if vtt is None:
                    continue
                turns = parse_vtt(vtt)
                docs.append(self._transcript_doc(mt, turns))
            logger.info("zoom: %d meeting transcripts", len(docs))
            return docs

    async def _download_transcript(self, client: httpx.AsyncClient, meeting: dict) -> str | None:
        for f in meeting.get("recording_files", []):
            if f.get("file_type") == "TRANSCRIPT" and f.get("download_url"):
                resp = await request_with_retries(client, "GET", f["download_url"])
                return resp.text
        return None

    def _transcript_doc(self, meeting: dict, turns: list[tuple[str, str]]) -> RawDoc:
        return RawDoc(
            source_type="meeting_transcript",
            external_id=str(meeting.get("uuid") or meeting.get("id")),
            resource_key=self.account_id,
            title=meeting.get("topic", "Zoom meeting"),
            url=meeting.get("share_url"),
            content=transcript_to_text(turns),
            raw_payload={"metadata": meeting, "turns": turns},
        )
