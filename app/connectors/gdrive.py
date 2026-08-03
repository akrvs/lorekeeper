import json
import logging
import time
from collections.abc import Iterable
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors._http import build_async_client, request_with_retries
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory

logger = logging.getLogger("company_brain.connectors.gdrive")

_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_FIELDS = (
    "nextPageToken,files(id,name,mimeType,modifiedTime,createdTime,webViewLink,owners(displayName))"
)
# Google-native types exported to text; plain text-ish files downloaded as-is.
_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
_TEXT_PREFIXES = ("text/",)
_TEXT_TYPES = {"application/json", "application/xml"}


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@ConnectorFactory.register("gdrive")
class GoogleDriveConnector(BaseConnector):
    source_system = "gdrive"

    def __init__(
        self,
        db: Session,
        *,
        folder_id: str | None = None,
        credentials_path: str | None = None,
        access_token: str | None = None,
        max_items: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_,
    ):
        super().__init__(db)
        self.credentials_path = credentials_path or settings.google_application_credentials
        self.access_token = access_token
        if not self.access_token and not self.credentials_path:
            raise ValueError(
                "GoogleDriveConnector requires service-account credentials "
                "(GOOGLE_APPLICATION_CREDENTIALS)."
            )
        self.folder_id = folder_id or settings.gdrive_folder_id
        self.resource_key = self.folder_id or "all"
        self.max_items = max_items or settings.ingest_max_items
        self._transport = transport
        self._since: str | None = None

    def fetch(self) -> Iterable[RawDoc]:
        self._since = self.last_cursor()
        return run_blocking(self._fetch_all())

    async def _token(self, client: httpx.AsyncClient) -> str:
        if self.access_token:
            return self.access_token
        import jwt  # PyJWT[crypto], already a dependency for RBAC JWKS

        with open(self.credentials_path, encoding="utf-8") as fh:
            creds = json.load(fh)
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": creds["client_email"],
                "scope": _SCOPE,
                "aud": creds["token_uri"],
                "iat": now,
                "exp": now + 3600,
            },
            creds["private_key"],
            algorithm="RS256",
        )
        resp = await request_with_retries(
            client,
            "POST",
            creds["token_uri"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        return resp.json()["access_token"]

    def _query(self) -> str:
        parts = ["trashed = false"]
        if self.folder_id:
            parts.append(f"'{self.folder_id}' in parents")
        if self._since:
            parts.append(f"modifiedTime > '{self._since}'")
        return " and ".join(parts)

    async def _fetch_all(self) -> list[RawDoc]:
        docs: list[RawDoc] = []
        async with build_async_client(
            settings.gdrive_api_url,
            headers={"User-Agent": "company-brain"},
            transport=self._transport,
        ) as client:
            client.headers["Authorization"] = f"Bearer {await self._token(client)}"
            page_token: str | None = None
            files: list[dict] = []
            while len(files) < self.max_items:
                params = {
                    "q": self._query(),
                    "orderBy": "modifiedTime",
                    "pageSize": 100,
                    "fields": _FIELDS,
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = await request_with_retries(client, "GET", "/drive/v3/files", params=params)
                payload = resp.json()
                files.extend(payload.get("files", []))
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
            files = files[: self.max_items]

            for meta in files:
                text = await self._content(client, meta)
                if text is None:
                    continue
                docs.append(self._build_doc(meta, text))

        stamps = [d.source_updated_at for d in docs if d.source_updated_at]
        if stamps:
            self.new_cursor = max(stamps).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            self.new_cursor = self._since
        logger.info("gdrive %s: %d documents", self.resource_key, len(docs))
        return docs

    async def _content(self, client: httpx.AsyncClient, meta: dict) -> str | None:
        mime = meta.get("mimeType", "")
        if mime in _EXPORTS:
            resp = await request_with_retries(
                client,
                "GET",
                f"/drive/v3/files/{meta['id']}/export",
                params={"mimeType": _EXPORTS[mime]},
            )
            return resp.text
        if mime.startswith(_TEXT_PREFIXES) or mime in _TEXT_TYPES:
            resp = await request_with_retries(
                client, "GET", f"/drive/v3/files/{meta['id']}", params={"alt": "media"}
            )
            return resp.text
        return None  # binaries (images, PDFs, ...) are skipped

    def _build_doc(self, meta: dict, text: str) -> RawDoc:
        name = meta.get("name") or meta["id"]
        owners = meta.get("owners") or []
        return RawDoc(
            source_type="drive_file",
            external_id=meta["id"],
            resource_key=self.resource_key,
            title=name,
            url=meta.get("webViewLink"),
            author=(owners[0].get("displayName") if owners else None),
            content=f"Google Drive file: {name}\n\n{text}",
            raw_payload=meta,
            source_created_at=_ts(meta.get("createdTime")),
            source_updated_at=_ts(meta.get("modifiedTime")),
        )
