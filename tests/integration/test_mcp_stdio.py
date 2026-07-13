"""Full MCP stdio round-trip: a real client drives `python -m app.mcp.server`."""

import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import select

from app.db.models import Node
from app.db.session import SessionLocal

_EXPECTED_TOOLS = [
    "approve_proposal",
    "get_node_details",
    "get_node_neighbors",
    "reject_proposal",
    "review_proposals",
    "rollback_proposal",
    "semantic_search",
    "traverse_graph_path",
]


async def test_stdio_handshake_and_tool_call(committed_graph):
    with SessionLocal() as db:
        feature_id = str(db.scalar(select(Node).where(Node.node_type == "feature")).id)

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp.server"], env={**os.environ}
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = sorted(t.name for t in (await session.list_tools()).tools)
            assert tools == _EXPECTED_TOOLS

            result = await session.call_tool("get_node_details", {"node_id": feature_id})
            payload = result.content[0].text
            assert "checkout-v2" in payload
            assert "slack.com/archives" in payload
