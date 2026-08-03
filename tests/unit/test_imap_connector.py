"""IMAP connector against a fake mailbox: no network, real imaplib semantics."""

from sqlalchemy import select, text

from app.connectors import ImapConnector
from app.db.models import RawDocument

_PLAIN = (
    b"Subject: Outage postmortem\r\n"
    b"From: Alice <alice@acme.com>\r\n"
    b"To: team@acme.com\r\n"
    b"Date: Mon, 01 Jun 2026 10:00:00 +0000\r\n"
    b"Message-ID: <m1@acme.com>\r\n"
    b"Content-Type: text/plain\r\n\r\n"
    b"Root cause: bad deploy of checkout-v2."
)
_HTML = (
    b"Subject: Action items\r\n"
    b"From: Bob <bob@acme.com>\r\n"
    b"To: team@acme.com\r\n"
    b"Date: Tue, 02 Jun 2026 09:00:00 +0000\r\n"
    b"Message-ID: <m2@acme.com>\r\n"
    b"Content-Type: text/html\r\n\r\n"
    b"<p>Rotate keys &amp; add alerts.</p>"
)


class FakeIMAP:
    """Just enough of imaplib's uid()/select() surface for the connector."""

    def __init__(self, messages: dict[int, bytes]):
        self.messages = messages

    def select(self, folder, readonly=False):
        return "OK", [b""]

    def uid(self, command, *args):
        if command == "SEARCH":
            start = int(args[1].split()[1].split(":")[0])
            uids = [u for u in sorted(self.messages) if u >= start]
            if not uids and self.messages:  # IMAP quirk: 'N:*' returns the newest
                uids = [max(self.messages)]
            return "OK", [b" ".join(str(u).encode() for u in uids)]
        if command == "FETCH":
            return "OK", [(b"", self.messages[int(args[0])])]
        raise AssertionError(f"unexpected command {command}")

    def logout(self):
        return "OK", [b""]


def _connector(db, messages):
    return ImapConnector(db, host="mail", user="u", password="p", client=FakeIMAP(messages))


def test_imap_fetch_bodies_and_cursor(db):
    db.execute(text("TRUNCATE raw_documents, ingestion_runs RESTART IDENTITY CASCADE"))
    db.commit()

    run1, docs1 = _connector(db, {3: _PLAIN, 5: _HTML}).run()
    assert len(docs1) == 2
    assert run1.cursor == "5"
    plain = db.scalar(select(RawDocument).where(RawDocument.external_id == "<m1@acme.com>"))
    assert plain.source_system == "imap"
    assert "Root cause: bad deploy" in plain.content
    assert plain.author == "Alice <alice@acme.com>"
    assert plain.source_created_at is not None
    html = db.scalar(select(RawDocument).where(RawDocument.external_id == "<m2@acme.com>"))
    assert "Rotate keys & add alerts." in html.content
    assert "<p>" not in html.content

    # Second run: nothing new above the cursor; the 'N:*' echo is filtered out.
    run2, docs2 = _connector(db, {3: _PLAIN, 5: _HTML}).run()
    assert docs2 == []
    assert run2.cursor == "5"


def test_imap_requires_configuration(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "imap_host", None)
    try:
        ImapConnector(db)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "IMAP_HOST" in str(exc)
