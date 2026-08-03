"""Data ingestion connectors.

Importing this package registers every driver with the ConnectorFactory (via the
`@ConnectorFactory.register` decorators). Network I/O is async (httpx) with
retry/backoff; the BaseConnector contract stays sync so the DB + extraction
layers are unchanged.
"""

from app.connectors._http import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorHTTPError,
)
from app.connectors.base import BaseConnector, RawDoc

# Import drivers for their registration side effects.
from app.connectors.confluence import ConfluenceConnector
from app.connectors.factory import ConnectorFactory
from app.connectors.gdrive import GoogleDriveConnector
from app.connectors.github import GitHubConnector
from app.connectors.gmeet import GoogleMeetConnector
from app.connectors.imap import ImapConnector
from app.connectors.jira import JiraConnector
from app.connectors.linear import LinearConnector
from app.connectors.local import LocalConnector
from app.connectors.notion import NotionConnector
from app.connectors.slack import SlackConnector
from app.connectors.teams import TeamsConnector
from app.connectors.zoom import ZoomConnector

__all__ = [
    "BaseConnector",
    "RawDoc",
    "ConnectorFactory",
    "ConfluenceConnector",
    "GitHubConnector",
    "GoogleDriveConnector",
    "ImapConnector",
    "SlackConnector",
    "JiraConnector",
    "LinearConnector",
    "NotionConnector",
    "LocalConnector",
    "TeamsConnector",
    "ZoomConnector",
    "GoogleMeetConnector",
    "ConnectorError",
    "ConnectorAuthError",
    "ConnectorHTTPError",
]
