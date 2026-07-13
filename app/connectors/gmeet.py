"""Google Meet connector (Track 4) — registered scaffold.

Meet transcript access depends on the deployment (Workspace admin export, the
Drive file the transcript lands in, or the Vault/Reports API), so the network
wiring is intentionally left as the integration point. The transcript mapping
itself is done — `_transcript_doc` turns parsed turns into a RawDoc — so finishing
this driver is just fetching the transcript text and calling it.
"""

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.config import settings
from app.connectors._transcript import transcript_to_text
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory


@ConnectorFactory.register("gmeet")
class GoogleMeetConnector(BaseConnector):
    source_system = "gmeet"

    def __init__(self, db: Session, *, credentials_json: str | None = None, **_):
        super().__init__(db)
        self.credentials_json = credentials_json or settings.gmeet_credentials_json

    def fetch(self) -> Iterable[RawDoc]:
        return run_blocking(self._fetch_all())

    async def _fetch_all(self) -> list[RawDoc]:
        # Integration point: authenticate with a Workspace service account and
        # enumerate Meet conference transcripts (Drive/Vault), then for each:
        #     turns = parse_vtt(transcript_text)
        #     docs.append(self._transcript_doc(meeting, turns))
        raise NotImplementedError(
            "GoogleMeetConnector is a staged scaffold — wire up transcript retrieval "
            "(Workspace service account / Drive export) then use _transcript_doc()."
        )

    def _transcript_doc(self, meeting: dict, turns: list[tuple[str, str]]) -> RawDoc:
        return RawDoc(
            source_type="meeting_transcript",
            external_id=str(meeting.get("conferenceId", meeting.get("id", ""))),
            resource_key=meeting.get("space", "gmeet"),
            title=meeting.get("title", "Google Meet"),
            url=meeting.get("url"),
            content=transcript_to_text(turns),
            raw_payload={"metadata": meeting, "turns": turns},
        )
