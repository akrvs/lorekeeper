# Company Brain

[![CI](https://github.com/your-org/company-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/company-brain/actions/workflows/ci.yml)
&nbsp;Python 3.12 · PostgreSQL 16 + pgvector · FastAPI · Model Context Protocol

> **An open-source organizational memory layer for AI agents.** Company Brain
> continuously extracts entities and relationships from fragmented sources
> (GitHub, Slack, …) into an **ontology-backed knowledge graph** in PostgreSQL,
> and exposes it to coding assistants (Cursor, Claude Desktop, Windsurf, Gemini)
> over the **Model Context Protocol (MCP)** — no UI required.

---

## Why not just vector RAG?

Standard retrieval-augmented generation embeds chunks of text and retrieves the
*most similar* ones. That works for "find me a paragraph about X" but collapses
on organizational questions, because the answer lives in **relationships**, not
similarity:

> *"Which Slack threads discuss the feature that caused the deployment failure in
> this repo?"*

A vector store has no notion that a **pull request** *implements* a **feature**
that *caused* an **incident** that a **Slack thread** *discusses*. Company Brain
models those edges explicitly, so an agent can **traverse** from a repository to
the human conversations about an outage — and every hop is backed by a real
source document it can cite.

| | Vector RAG | Company Brain |
|---|---|---|
| Unit of knowledge | Text chunk | Typed **entity** + typed **relationship** |
| Cross-source links | None (separate embeddings) | Entities **deduplicated & merged** across GitHub/Slack/… |
| "How is A connected to B?" | Not answerable | **Multi-hop graph traversal** |
| Provenance | Chunk → document | Every node/edge → the exact source artifact + URL |
| Retrieval | Cosine similarity only | Semantic search **and** graph queries |

Company Brain keeps semantic search (pgvector) **and** adds the graph — you get
both.

---

## High-level architecture

```
  Connectors              Ontology Engine              Knowledge Graph             Agents
  (async httpx)          (Azure OpenAI)                (Postgres + pgvector)        (MCP)
┌────────────┐  raw    ┌────────────────────────┐    ┌──────────────────────┐    ┌──────────┐
│ GitHub     │ ──────▶ │ extract entities/edges  │──▶ │ nodes   (+ embedding)│ ◀─ │ MCP      │ ◀─ Cursor /
│ Slack      │  docs   │ embed + deduplicate     │    │ edges   (typed)      │    │ server   │    Claude /
│ Notion…    │ ──────▶ │ map onto the ontology   │──▶ │ raw_documents        │    │ (stdio)  │    Windsurf
└────────────┘         └────────────────────────┘    │ node_mentions        │    └──────────┘
       │                          │                   └──────────────────────┘         │
       └─ raw_documents ──────────┴── provenance links ──────────┴── cite sources ─────┘
```

### The hybrid property-graph schema

Rather than reach for a dedicated graph database on day one, Company Brain models
a **property graph inside PostgreSQL** — relational rigor, ACID, `pgvector` ANN
search, and a clean migration path to Neo4j/Memgraph later. Seven tables:

| Table | Purpose |
|---|---|
| `ontology_node_types` | Registry of valid entity types (the ontology *is data*) |
| `ontology_relationship_types` | Registry of valid relationship types + legal endpoints |
| `raw_documents` | Immutable landing zone for every ingested artifact (full payload + URL) |
| `ingestion_runs` | Connector run bookkeeping (status, cursor, stats) |
| `nodes` | Every entity — `node_type` + JSONB `properties` + `embedding vector(1536)` |
| `edges` | Directed, typed relationships (`source_id`→`target_id`, `relationship`, evidence) |
| `node_mentions` | Provenance bridge: which `raw_documents` a node was extracted from |

Three design decisions make this work:

1. **One `nodes` table, not table-per-type.** Graph traversal is uniform self-joins
   on `edges`; one HNSW index (`vector_cosine_ops`) serves semantic search across
   *all* entity kinds.
2. **The ontology is data, not an `ENUM`.** `nodes.node_type` / `edges.relationship`
   are foreign keys into the registry tables, and the LLM's structured-output enums
   are generated *from the same registry* — so the model can never emit a type the
   database would reject. Extending the ontology is an `INSERT`, not an `ALTER TYPE`.
3. **Everything is traceable.** Nodes link to `node_mentions`; edges carry an
   `evidence_document_id`. The graph never asserts anything it can't point at.

### Tech stack

- **PostgreSQL 16 + pgvector** — hybrid relational/graph store with HNSW ANN search
- **Python 3.12 · FastAPI · SQLAlchemy 2.0** — backend, connectors, extraction
- **Azure OpenAI** — structured entity extraction (`beta.chat.completions.parse`) + embeddings
- **Alembic** — versioned schema migrations applied on startup
- **MCP (`mcp` SDK / FastMCP)** — the agent-facing interface (stdio)
- **Docker Compose** — one command, local or straight-to-VM

---

## Try it in 30 seconds

Zero config, zero API keys, zero cost — just Docker:

```bash
make demo
```

This spins up a `pgvector` database, ingests the bundled `demo_vault/` (an
interconnected set of incident/architecture/runbook notes) using the **offline
`stub` extractor**, and prints the resolved knowledge graph — every `[[wikilink]]`
resolved to a real file node. Because the `stub` provider generates deterministic
embeddings and a rule-based extraction locally, you evaluate the full pipeline
(scan → extract → embed → resolve → graph) with **no Azure/OpenAI spend**.

The demo leaves a database running on `localhost:5432` and prints three natural-
language queries to try from an MCP client (Cursor / Claude Desktop):

> 1. *"Search Company Brain for the checkout-v2 architecture, then show me which documents reference it."*
> 2. *"What does the incident report connect to? Show its neighbors."*
> 3. *"Trace a path from the incident report to the api-keys-policy doc."*

Tear it down with `make demo-down`. Then read on for the full stack.

## Quickstart

### Prerequisites
- Docker Engine + the Compose plugin
- (For real extraction) an Azure OpenAI resource; (for live data) GitHub/Slack tokens

### 1. Configure
```bash
cp .env.example .env
# Edit .env:
#   LLM_PROVIDER=azure  + AZURE_OPENAI_*   (or LLM_PROVIDER=stub for an offline trial)
#   GITHUB_TOKEN / GITHUB_REPO
#   SLACK_BOT_TOKEN / SLACK_CHANNEL_ID
```

### 2. Run the stack (development)
```bash
make up                          # build + start db and api (schema auto-migrates)
curl localhost:8000/health/db    # => graph + ontology counts
```
On startup the API waits for the database, applies `alembic upgrade head`, and
seeds the ontology — the system is queryable immediately.

### 3. Ingest real data
```bash
# Turnkey CLI (the one-shot live sync):
make sync-github REPO=owner/name
make sync-slack  CHANNEL=C0123456789
#   → docker compose run --rm -T api python -m app.pipeline --source github --repo owner/name

# …or via the API:
curl -X POST "localhost:8000/ingest/github?repo=owner/name"
curl -X POST "localhost:8000/ingest/slack?channel_id=C0123456789"
```
> **Zero-cost dry run:** set `LLM_PROVIDER=stub`. Connectors hit the **real**
> GitHub/Slack APIs and populate `raw_documents`; extraction is skipped (no Azure
> spend). Flip to `azure` to build the graph.

### 4. Connect an AI agent (MCP)
```bash
make mcp     # docker compose run --rm -T mcp   — what an MCP client launches
```
Register the server in Claude Desktop / Cursor — see
[`docs/MCP_SETUP.md`](docs/MCP_SETUP.md) for the exact `claude_desktop_config.json`.

### Production deployment (fresh Linux VM)
```bash
# 1. Install Docker + Compose
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker

# 2. Clone + configure
git clone <your-repo-url> company-brain && cd company-brain
cp .env.example .env && nano .env          # set secrets (POSTGRES_PASSWORD, AZURE_*, tokens)

# 3. Launch the hardened production stack (db has NO exposed port; api auto-migrates)
docker compose -f docker-compose.prod.yml up --build -d
docker compose -f docker-compose.prod.yml ps
curl -s localhost:8000/health/db

# 4. Sync data
docker compose -f docker-compose.prod.yml run --rm -T api \
    python -m app.pipeline --source github --repo owner/name
```
Upgrades: `git pull && docker compose -f docker-compose.prod.yml up --build -d` —
new migrations apply automatically on startup.

---

## MCP tools

The MCP server exposes four tools that return **structured, LLM-parseable text**:

| Tool | What it does |
|---|---|
| `semantic_search(query, node_type?, limit?)` | ANN over node embeddings (pgvector cosine) → ranked entities + UUIDs |
| `get_node_details(node_id)` | Attributes, JSONB properties, and the **source documents/URLs** behind a node |
| `get_node_neighbors(node_id, direction?)` | 1-hop edges + neighbor nodes (incoming/outgoing/both) |
| `traverse_graph_path(start_node_id, target_node_type, max_depth=3)` | Multi-hop paths via a recursive CTE (e.g. repository → slack_thread via incident) |

A typical agent flow: `semantic_search` → `traverse_graph_path` /
`get_node_neighbors` → `get_node_details` (to cite sources).

---

## Security posture & graph-level RBAC

> **Status:** graph-level RBAC is **implemented and defaults OFF** (`RBAC_ENABLED=false`),
> so the open-source MVP is unrestricted out of the box. Turn it on to row-filter
> every MCP tool by the caller's granted data sources — enforced entirely at the
> MCP layer via `GraphRepository`, with no connector or schema changes.

The graph carries everything authorization needs: every `raw_documents` row records
its `source_system` and `resource_key` (the GitHub repo, the Slack/Teams channel id),
and every node links back to its documents via `node_mentions`. That provenance is
the ACL anchor. How enforcement works (`app/security/`):

1. **Identity propagation.** Run the MCP server over an authenticated transport
   (HTTP/SSE with `Authorization: Bearer <token>`, or inject a short-lived token
   into the stdio launch). The token is an **OIDC/JWT issued by your IdP** (Okta,
   Microsoft Entra ID, Google Workspace).
2. **Principal resolution.** The server validates the JWT against the IdP's JWKS
   and extracts the principal — user id, groups/roles, team memberships.
3. **Scope mapping.** Map IdP groups → the set of `source_system` + repo/channel
   the principal may see (a policy table, or the graph's own
   `user —MEMBER_OF→ team —OWNS→ repository` edges).
4. **Query-time filtering (single choke point).** Wrap all four tools in one
   authorization dependency that injects a `WHERE` scope:
   - `semantic_search` / `traverse_graph_path` restrict candidate nodes to the
     allowed sources;
   - `get_node_details` / source lookups filter `node_mentions → raw_documents`
     to documents from channels/repos the principal can access.
   Because everything funnels through one `nodes`/`edges`/`raw_documents` model,
   authorization is enforced in **one place**, not per-connector.
5. **Audit.** Log every tool call with the resolved principal and the applied scope.

This keeps the boundary where it belongs — the agent only ever receives nodes,
edges, and source URLs its human operator is already entitled to read.

Operational hygiene already in place: secrets are read only from the environment
(`app/config.py`), never hardcoded; the production DB has **no exposed port**
(private to the Docker network); the image runs as a **non-root** user; and the
MCP server logs strictly to stderr (stdout is the protocol channel).

---

## Open-core extension model — write a connector

The platform is connector-pluggable. To add a source you implement **one method**:
fetch artifacts and yield `RawDoc`s. Everything downstream — storage, extraction,
embedding, dedup, graph building, MCP exposure — is handled for you.

```python
# app/connectors/notion.py
from collections.abc import Iterable

import httpx

from app.config import settings
from app.connectors._http import build_async_client, request_with_retries
from app.connectors.base import BaseConnector, RawDoc, run_blocking
from app.connectors.factory import ConnectorFactory


@ConnectorFactory.register("notion")   # self-registers — no other file changes
class NotionConnector(BaseConnector):
    source_system = "notion"

    def __init__(self, db, *, database_id=None, token=None, transport=None):
        super().__init__(db)
        self.database_id = database_id or settings.notion_database_id
        self.token = token or settings.notion_token
        if not self.database_id or not self.token:
            raise ValueError("NotionConnector requires a database_id and token.")
        self._transport = transport

    # Sync bridge required by BaseConnector (handles the IngestionRun + upserts).
    def fetch(self) -> Iterable[RawDoc]:
        return run_blocking(self._fetch_all())

    async def _fetch_all(self) -> list[RawDoc]:
        client = build_async_client(
            "https://api.notion.com/v1",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": "2022-06-28",
            },
            transport=self._transport,   # injectable httpx.MockTransport for tests
        )
        async with client:
            resp = await request_with_retries(   # exp. backoff + 429 handling, free
                client, "POST", f"/databases/{self.database_id}/query"
            )
            return [
                RawDoc(
                    source_type="page",
                    external_id=page["id"],
                    title=page["properties"]["Name"]["title"][0]["plain_text"],
                    url=page["url"],
                    content=_render_page(page),
                    raw_payload=page,
                )
                for page in resp.json()["results"]
            ]
```

Then:
1. Import the module once (e.g. in `app/connectors/__init__.py`) so the
   `@ConnectorFactory.register` decorator runs — the pipeline, the `/ingest` API,
   and the `--source` CLI discover it automatically. **No existing file changes.**
2. (Optional) add any new `node_type`/`relationship` terms to `app/db/ontology_seed.py`.
3. Add a pytest using `httpx.MockTransport` under `tests/` — CI runs it against a
   live `pgvector` container.

That's it. The `BaseConnector` contract (`RawDoc` → `raw_documents`), the Azure
extraction layer, the trigram/cosine resolver, and the MCP tools require **no
changes** to support a new source.

> **Ships with seven drivers:** GitHub, Slack, Notion, Local/Obsidian (parallel
> filesystem scan), plus registered Microsoft Teams, Zoom, and Google Meet
> scaffolds with a shared transcript engine.

---

## Testing & CI

The suite runs against a **real** `pgvector` database (no mocks for the graph
layer); connectors are exercised with `httpx.MockTransport` replaying captured API
payloads (including a 429 to prove backoff). `.github/workflows/ci.yml` runs on
every push/PR to `main`/`develop`:

| Stage | What it guarantees |
|---|---|
| `ruff check` / `ruff format --check` | No lint, import, or formatting drift (fails fast) |
| `python -m app.db.init_db` | Alembic migrations apply on a clean DB |
| `connector_check` | Pagination, 429 backoff, comment/thread assembly |
| `integration_check` | Ingest → cross-source dedup → provenance → headline traversal |
| `mcp_check` / `mcp_stdio_check` | All four tools + a real MCP stdio handshake |

Run them locally:
```bash
pip install -r requirements-dev.txt && ruff check . && ruff format --check .
# (with a Postgres reachable via the POSTGRES_* env vars and LLM_PROVIDER=stub)
python -m app.db.init_db && python tests/integration_check.py
```

---

## Project structure

```
app/
  config.py            # centralized env-driven settings (no hardcoded secrets)
  main.py              # FastAPI app: lifespan migrations + /health + /ingest
  pipeline.py          # connector → extract → embed → resolve; CLI entrypoint
  connectors/          # BaseConnector + async GitHub/Slack + _http (retry/backoff)
  llm/                 # provider abstraction: Azure OpenAI + offline stub
  ontology/            # extraction schema, embeddings, resolver (dedup)
  mcp/server.py        # FastMCP server (stdio) — the 4 agent tools
  db/                  # models, session, Alembic-based bootstrap, ontology seed
alembic/               # migration environment + versions/0001_initial.py
docs/MCP_SETUP.md      # Claude Desktop / Cursor registration
tests/                 # connector / integration / MCP verification suite
docker-compose.yml         # development stack
docker-compose.prod.yml    # hardened production stack
.github/workflows/ci.yml   # lint + migrate + test
```

---

## License

Released under the Apache License 2.0. Add a `LICENSE` file before publishing.
