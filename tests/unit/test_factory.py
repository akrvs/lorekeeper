"""ConnectorFactory registry."""

import pytest

from app.connectors import ConnectorFactory


def test_all_drivers_registered():
    available = ConnectorFactory.available()
    for source in ("github", "slack", "notion", "local", "teams", "zoom", "gmeet"):
        assert source in available


def test_unknown_source_raises():
    with pytest.raises(ValueError, match="Unknown source"):
        ConnectorFactory.create(None, "does-not-exist")
