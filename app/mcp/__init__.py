"""MCP server.

Exposes graph tools to coding agents (Cursor, Claude Desktop, Windsurf, Gemini)
over the Model Context Protocol via the official `mcp` SDK (FastMCP, stdio).

Entrypoint: `python -m app.mcp.server`

Tools:
    semantic_search       — ANN over node embeddings (pgvector cosine)
    get_node_details      — attributes + JSONB properties + source documents
    get_node_neighbors    — 1-hop edges and neighbor nodes (directional)
    traverse_graph_path   — multi-hop paths via a recursive CTE over edges
"""
