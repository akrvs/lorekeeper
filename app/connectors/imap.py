import email
import email.header
import imaplib
import logging
from collections.abc import Iterable
from email.message import Message
from email.utils import parsedate_to_datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.base import BaseConnector, RawDoc
from app.connectors.factory import ConnectorFactory
from app.textutil import strip_html

logger = logging.getLogger("company_brain.connectors.imap")


def _decode_header(value: str | None) -> str:
    parts = email.header.decode_header(value or "")
    return "".join(
        part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part
        for part, enc in parts
    )


def _body_text(msg: Message) -> str:
    """Prefer the text/plain part; fall back to stripped text/html."""
    if msg.is_multipart():
        plain = html = None
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            if part.get_content_type() == "text/plain" and plain is None:
                plain = text
            elif part.get_content_type() == "text/html" and html is None:
                html = text
        return plain if plain is not None else strip_html(html or "")
    payload = msg.get_payload(decode=True)
    text = payload.decode(msg.get_content_charset() or "utf-8", "replace") if payload else ""
    return strip_html(text) if msg.get_content_type() == "text/html" else text


@ConnectorFactory.register("imap")
class ImapConnector(BaseConnector):
    source_system = "imap"

    def __init__(
        self,
        db: Session,
        *,
        host: str | None = None,
        user: str | None = None,
        password: str | None = None,
        folder: str | None = None,
        port: int | None = None,
        max_items: int | None = None,
        client=None,
        **_,
    ):
        super().__init__(db)
        self.host = host or settings.imap_host
        self.user = user or settings.imap_user
        self.password = password or settings.imap_password
        if not self.host or not self.user or not self.password:
            raise ValueError(
                "ImapConnector requires credentials (IMAP_HOST, IMAP_USER, IMAP_PASSWORD)."
            )
        self.folder = folder or settings.imap_folder
        self.port = port or settings.imap_port
        self.resource_key = f"{self.user}/{self.folder}"
        self.max_items = max_items or settings.ingest_max_items
        self._client = client  # test seam: anything speaking imaplib's uid()/select()

    def _connect(self):
        if self._client is not None:
            return self._client
        client = imaplib.IMAP4_SSL(self.host, self.port)
        client.login(self.user, self.password)
        return client

    def fetch(self) -> Iterable[RawDoc]:
        last_uid = int(self.last_cursor() or 0)
        client = self._connect()
        try:
            client.select(self.folder, readonly=True)
            _typ, data = client.uid("SEARCH", None, f"UID {last_uid + 1}:*")
            found = [int(u) for u in (data[0].split() if data and data[0] else [])]
            # 'N:*' returns the newest message even when N exceeds it — filter.
            uids = sorted(u for u in found if u > last_uid)[: self.max_items]
            docs: list[RawDoc] = []
            top = last_uid
            for uid in uids:
                typ, msg_data = client.uid("FETCH", str(uid), "(RFC822)")
                if typ != "OK" or not msg_data or msg_data[0] is None:
                    continue
                docs.append(self._build_doc(uid, email.message_from_bytes(msg_data[0][1])))
                top = max(top, uid)
            self.new_cursor = str(top) if top else None
            logger.info("imap %s: %d messages", self.resource_key, len(docs))
            return docs
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 — a failed logout must not fail the run
                pass

    def _build_doc(self, uid: int, msg: Message) -> RawDoc:
        subject = _decode_header(msg.get("Subject"))
        sender = _decode_header(msg.get("From"))
        try:
            stamp = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
        except (TypeError, ValueError):
            stamp = None
        content = (
            f"Email: {subject}\nFrom: {sender}\n"
            f"To: {_decode_header(msg.get('To'))}\n\n{_body_text(msg)}"
        )
        return RawDoc(
            source_type="email",
            external_id=msg.get("Message-ID") or f"{self.folder}:{uid}",
            resource_key=self.resource_key,
            title=subject or f"(no subject) uid={uid}",
            author=sender,
            content=content,
            raw_payload={k: str(v) for k, v in msg.items()},
            source_created_at=stamp,
            source_updated_at=stamp,
        )
