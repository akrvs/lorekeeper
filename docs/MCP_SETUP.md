# Registering the Company Brain MCP server

The server speaks MCP over **stdio**, which is what Claude Desktop, Cursor and
Windsurf launch. You register a *command* that the client spawns; the client
owns that process's stdin/stdout for the JSON-RPC stream.

Prereqs: the database must be running and populated.

```bash
cp .env.example .env            # set AZURE_OPENAI_* for real semantic_search
docker compose up --build -d    # starts db + api (api bootstraps the schema)
curl -X POST localhost:8000/ingest/github   # populate the graph
curl -X POST localhost:8000/ingest/slack
```

> `semantic_search` embeds the query with the configured provider. With
> `LLM_PROVIDER=azure` (default) set your `AZURE_OPENAI_*` vars. For offline use
> set `LLM_PROVIDER=stub` — the other three tools work fully regardless.

---

## Option A — Docker (recommended; no local Python)

The client launches the `mcp` compose service on demand. `db` must already be
up (`docker compose up -d db api`).

### Claude Desktop
Edit `claude_desktop_config.json`:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "company-brain": {
      "command": "docker",
      "args": [
        "compose",
        "-f", "/absolute/path/to/company-brain/docker-compose.yml",
        "run", "--rm", "-T", "mcp"
      ]
    }
  }
}
```

`-T` disables TTY allocation (required — the protocol is a raw byte stream).
Use the **absolute** path to your `docker-compose.yml`.

### Cursor
Settings → **Cursor Settings** → **MCP** → **Add new global MCP server**, then add
the same JSON under `mcpServers` (Cursor reads `~/.cursor/mcp.json`).

---

## Option B — Local Python process

Install deps and point the server at the Dockerized Postgres on `localhost`.

```bash
pip install -r requirements.txt
```

```json
{
  "mcpServers": {
    "company-brain": {
      "command": "python",
      "args": ["-m", "app.mcp.server"],
      "cwd": "/absolute/path/to/company-brain",
      "env": {
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "brain",
        "POSTGRES_PASSWORD": "brain",
        "POSTGRES_DB": "company_brain",
        "LLM_PROVIDER": "stub"
      }
    }
  }
}
```

Set `LLM_PROVIDER=azure` plus the `AZURE_OPENAI_*` vars for real semantic search.

---

## Verifying

Restart the client. You should see the **company-brain** server with four tools:
`semantic_search`, `get_node_details`, `get_node_neighbors`, `traverse_graph_path`.

Try: *"Search Company Brain for the checkout feature, then show me which Slack
threads discuss the feature that caused the deploy failure in the
checkout-service repo."* The agent will chain `semantic_search` →
`traverse_graph_path` / `get_node_neighbors` → `get_node_details` and cite the
source URLs.

### Troubleshooting
- **Server won't start / no tools:** confirm `db` is up (`docker compose ps`) and
  reachable; check the client's MCP logs (Claude Desktop: *View → MCP logs*).
- **Garbled protocol errors:** something wrote to stdout. This server logs only
  to stderr; if you forked it, keep stdout clean.
- **`semantic_search` returns an embedding error:** provider not configured —
  set `LLM_PROVIDER=stub` or the `AZURE_OPENAI_*` vars.
