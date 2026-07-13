# Company Brain — Session Handoff Blueprint

> Hyper-dense context for resuming cold. Read this top-to-bottom and you have the
> whole system. Last frozen: end of Phase 9. State: **release-ready, green.**

## 0. What it is (one breath)
Open-source **organizational memory for AI agents**. Continuously extracts
entities + relationships from fragmented sources (GitHub, Slack, Notion, local
files, …) into an **ontology-backed property graph in PostgreSQL + pgvector**,
and exposes it to coding agents (Cursor/Claude Desktop/Windsurf) **only via MCP
(stdio)**. No UI. Beats plain vector RAG because it models *relationships* and
keeps full provenance (every node/edge → a real source document + URL).

## 1. Stack & entry points
- **Python 3.12 · FastAPI · SQLAlchemy 2.0 · Alembic · Postgres 16 + pgvector (HNSW, `vector_cosine_ops`)**
- **LLM:** Azure OpenAI (`client.beta.chat.completions.parse`, openai SDK pinned `>=1.59,<2`) + offline `stub`.
- **MCP:** official `mcp` SDK, `FastMCP`, **stdio** transport.
- Entry points:
  - `app/main.py` — FastAPI (lifespan runs migrations+seed); `POST /ingest/{source}`, `/health/db`.
  - `app/mcp/server.py` — MCP server, 4 tools (`python -m app.mcp.server`).
  - `app/pipeline.py` — `run_source()` + CLI (`python -m app.pipeline --source … --repo/--channel`).
  - `app/demo.py` — `make demo` runner.
  - `app/db/init_db.py` — `wait_for_db → alembic upgrade head → seed_ontology`.

## 2. Repo layout (the parts that matter)
```
app/
  config.py            # pydantic-settings singleton; ALL env lives here
  main.py  pipeline.py  demo.py
  db/      models/{ontology,source,node,edge,mention,security}.py · init_db.py · ontology_seed.py · session.py · base.py
  connectors/  base.py(_upsert+run_blocking) factory.py _http.py(retry/backoff) _transcript.py
               github.py slack.py notion.py local.py(+reconcile_wikilinks) teams.py zoom.py gmeet.py
  llm/     base.py(Protocol) azure.py stub.py(deterministic+heuristic) __init__.py(get_llm_provider)
  ontology/ schema.py(strict Pydantic+enums) extractor.py embeddings.py(+embed_many) resolver.py(dedup)
  repositories/graph.py   # GraphRepository — THE read choke point (visibility + bounded CTE)
  security/  principal.py identity.py visibility.py audit.py
  cache/   __init__.py(get_cache,make_key) memory.py redis.py
alembic/versions/  0001_initial.py  0002_resource_key_audit_grants.py
tests/   conftest.py fixtures.py  unit/*  integration/*
demo_vault/  5 interlinked .md files
docs/  MCP_SETUP.md  BRANCH_PROTECTION.md  SESSION_HANDOFF.md
docker-compose.yml  docker-compose.prod.yml  Dockerfile  Makefile  .github/workflows/ci.yml
```

## 3. The 9 phases (current state)
| Ph | Shipped | Key artifacts |
|----|---------|---------------|
| 1 | Scaffolding + **hybrid graph schema** (7 tables, JSONB props, `vector(1536)`, HNSW + trigram idx) | `db/models/*`, `db/init/001_extensions.sql` |
| 2 | Extraction engine: Azure structured-output extractor; **resolver dedup** (sourced upsert + derived **trigram OR cosine** merge); provenance | `ontology/{schema,extractor,embeddings,resolver}.py`, `llm/azure.py` |
| 3 | **MCP server** (FastMCP stdio) — 4 tools | `mcp/server.py` |
| 4 | **Real async connectors** (httpx) GitHub/Slack; retry/backoff (429/403/5xx), Link/cursor pagination, payload truncation; ingest CLI | `connectors/_http.py,github.py,slack.py`, `textutil.py` |
| 5 | **Alembic** (create_all removed; upgrade-on-startup); hardened `docker-compose.prod.yml` | `alembic/`, `0001` |
| 6 | **CI** (lint+test, pgvector service container), Ruff gate, enterprise README | `.github/workflows/ci.yml`, `pyproject.toml` |
| 7 | **Enterprise hardening**: `GraphRepository` keystone; **context-aware RBAC row-filtering** (principal/visibility/audit, default OFF); pytest framework (rollback isolation); **perf** (embedding batching, bounded recursive CTE, TTL cache); **`ConnectorFactory` + 7 drivers** | `repositories/graph.py`, `security/*`, `cache/*`, `connectors/factory.py`, migration `0002` |
| 8 | Ontology: **`meeting`/`participant`** node types + **`PARTICIPATES_IN`**; local smoke test; branch-protection docs; **offline `stub` heuristic extractor** | `ontology_seed.py`, `llm/stub.py`, `docs/BRANCH_PROTECTION.md` |
| 9 | **Wikilink → file resolution** (`reconcile_wikilinks` merges `[[link]]` concepts into real file nodes); `demo_vault/`; **`make demo`** (self-contained, no Compose) | `connectors/local.py`, `app/demo.py`, `Makefile` |

## 4. Data model
7 core tables: `ontology_node_types`, `ontology_relationship_types`,
`raw_documents`, `ingestion_runs`, `nodes`, `edges`, `node_mentions`.
Phase 7 added `access_grants`, `audit_log`, and `raw_documents.resource_key`.
Migrations: **0001** (core + extensions `vector`/`pg_trgm` + HNSW/trigram idx) →
**0002** (resource_key + audit + grants). `alembic check` = **no drift**.
- **Ontology = data, not ENUM:** `nodes.node_type`/`edges.relationship` are FKs into
  the registry tables; the LLM's strict-output enums are generated from the same
  seed lists (`ontology_seed.py`) → model can't emit an unknown type.
  Current: **17 node types, 19 relationship types.**
- **RBAC anchor:** every node reaches the graph via `raw_documents` (which carries
  `source_system` + `resource_key`) and links back via `node_mentions`.

## 5. Core mechanics (precise)
- **Resolver** (`ontology/resolver.py`): sourced nodes upsert on
  `uq_nodes_identity(node_type,source_system,external_id)` (xmax trick = created vs
  updated). Derived nodes (`external_id IS NULL`) dedup via `_find_duplicate`:
  `similarity(name) ≥ 0.55` (pg_trgm) **OR** `embedding <=> v ≤ 0.15` (HNSW cosine).
  `_canonical_id` is cycle-safe (visited-set + depth cap). Edges upsert on
  `uq_edges_identity`; `weight += 1` on re-observe.
- **GraphRepository** (`repositories/graph.py`): the only read path. Injects
  `visible_node_ids(principal)` into `semantic_search`/`get_node`/`neighbors`/
  `node_sources`; `traverse()` = recursive CTE with `SET LOCAL statement_timeout`,
  PG16 `CYCLE`, depth/result caps, and visibility EXISTS-fragment on the neighbor.
- **RBAC** (`security/`): `Principal{subject,groups,grants,superuser}`;
  `RBAC_ENABLED=false` ⇒ `Principal.anonymous()` superuser (back-compat).
  When on: `IdentityResolver` decodes JWT claims → groups → `access_grants` rows →
  `SourceGrant(source_system, resources|None)`. Visibility rule: node visible iff
  mentioned in a `raw_documents` row whose `(source_system,resource_key)` is granted.
  `_decode` is **unverified base64** in core — override for JWKS in prod.
  `AuditLogger` writes one `audit_log` row per tool call (never breaks the query).
- **Perf**: `embed_many` batches all node texts across a run into
  `⌈N/EMBEDDING_BATCH_SIZE⌉` calls (default 256). Cache (`cache/`): memory default,
  Redis opt-in (`CACHE_BACKEND=redis`); keys include `Principal.scope_key()`
  (security invariant) + a `graph` namespace version bumped on every ingest.
  `semantic_search`/`get_node_neighbors` are cached; `traverse` is not.
- **Connectors**: `ConnectorFactory.register("name")` decorator → discovered by
  pipeline/API/CLI. 7 drivers: **github, slack, notion, local** (full) + **teams,
  zoom, gmeet** (registered scaffolds + shared `_transcript.parse_vtt`). Sync
  `fetch()` bridges to async via `run_blocking` (thread-offloads if a loop runs).
  All set `resource_key`. Local does parallel FS scan + `reconcile_wikilinks`.
- **Offline `stub`** (`llm/stub.py`): deterministic embeddings (hash→seeded vec) +
  rule-based extraction (document node per artifact + `[[wikilink]]` REFERENCES).
  This is what `make demo` and CI use — **no Azure cost**.

## 6. The 4 MCP tools
All accept optional `principal_token`, route via `GraphRepository`, are audited.
`semantic_search(query, node_type?, limit?)` · `get_node_details(node_id)` ·
`get_node_neighbors(node_id, direction?)` · `traverse_graph_path(start, target_type, max_depth=3)`.
Return structured, LLM-parseable **text**.

## 7. Verification status (frozen)
- **`pytest tests/` → 31 passed** (unit: backoff 429/503, resolver cycle+merge,
  cache TTL, factory, transcript, visibility; integration: pipeline dedup+traversal,
  4 MCP tools, MCP **stdio handshake**, RBAC filtering, identity/JWT, connectors
  resilience, **wikilink resolution**). Run on a live `pgvector/pgvector:pg16`.
- **Ruff**: `ruff check` + `ruff format --check` clean across **68 files**.
- **Migrations**: `0001`+`0002` apply on a fresh DB; `alembic check` = no drift;
  baked image is self-contained (`python -m app.db.init_db`, no mount).
- **`make demo`**: 5 files → 5 file nodes → 9 edges → **0 concept nodes** (all
  wikilinks resolved).
- **CI** (`.github/workflows/ci.yml`): `lint` job (ruff) → `test` job (pgvector
  service, `python -m app.db.init_db`, `pytest tests/ -v`). Gated `main`/`develop`.

## 8. How to run (this is the muscle memory)
```bash
make demo            # 30-sec offline demo (own DB on :5432) ; make demo-down to clean
make up              # dev stack (db+api); /health/db ; make ingest SOURCE=local|github|slack
make sync-github REPO=owner/name        # live CLI ingest (needs tokens + LLM_PROVIDER=azure)
make mcp             # launch MCP server over stdio (what a client runs)
docker compose -f docker-compose.prod.yml up --build -d   # production (db has no exposed port)
```
Config: `cp .env.example .env`. Offline trial = `LLM_PROVIDER=stub`. Real graph =
`LLM_PROVIDER=azure` + `AZURE_OPENAI_*`; live data = `GITHUB_TOKEN/REPO`,
`SLACK_BOT_TOKEN/SLACK_CHANNEL_ID`, etc.

## 9. ⚠️ This dev host — gotchas for a fresh session
- **Not a git repo yet.** `git init` + first commit + push to GitHub is required
  before CI runs or branch protection (`docs/BRANCH_PROTECTION.md`) can apply.
- **Docker Compose plugin is NOT installed here.** All live verification used raw
  `docker run` on a manual network. The daemon may be stopped (`sudo systemctl
  start docker`); the user was added to the `docker` group (effective next login) —
  in a non-interactive shell prefix docker with `sg docker -c '…'`.
- **No Azure/GitHub/Slack/Teams/Zoom credentials** in this sandbox ⇒ those network
  paths are structurally complete but **not live-tested**. `gmeet` raises
  `NotImplementedError` until transcript retrieval is wired. No `LICENSE` file yet.
- **Verification recipe** (reproduce any check): start `pgvector/pgvector:pg16` on a
  docker network; run a one-off container with `-v $PWD:/work -w /work
  -e PYTHONPATH=/work -e LLM_PROVIDER=stub -e POSTGRES_HOST=<db>`; for pytest add
  `--user $(id -u):$(id -g) -e HOME=/tmp` and `pip install --user pytest
  pytest-asyncio pytest-mock`.

## 10. ▶ Immediate next steps for TOMORROW
**Goal A — see it live in Claude Desktop / Cursor (offline, fastest):**
1. `make demo` → leaves DB `company-brain-demo-db` on network `company-brain-demo-net`
   (+ `localhost:5432`), image `company-brain:latest` built.
2. Register the MCP server. **Most reliable on this host (no local Python deps):**
   add to `claude_desktop_config.json` (macOS:
   `~/Library/Application Support/Claude/`, Linux client similar):
   ```json
   {
     "mcpServers": {
       "company-brain": {
         "command": "docker",
         "args": ["run","--rm","-i","--network","company-brain-demo-net",
                  "-e","POSTGRES_HOST=company-brain-demo-db",
                  "-e","POSTGRES_USER=brain","-e","POSTGRES_PASSWORD=brain",
                  "-e","POSTGRES_DB=company_brain","-e","LLM_PROVIDER=stub",
                  "company-brain:latest","python","-m","app.mcp.server"]
       }
     }
   }
   ```
   (`-i` keeps stdin open for the JSON-RPC stream — equivalent to compose's `-T`.)
3. Restart the client → confirm **4 tools** appear → run the 3 sample queries from
   `make demo` output. Expect `traverse_graph_path(incident-report → api-keys-policy)`
   to return a real multi-hop path. See `docs/MCP_SETUP.md` for the local-Python and
   `docker compose run --rm -T mcp` variants.

**Goal B — real data:**
1. `.env`: `LLM_PROVIDER=azure`, `AZURE_OPENAI_ENDPOINT/API_KEY/*_DEPLOYMENT`
   (structured outputs need api-version ≥ 2024-08-01 + gpt-4o-2024-08-06+),
   `GITHUB_TOKEN`+`GITHUB_REPO`, `SLACK_BOT_TOKEN`+`SLACK_CHANNEL_ID`.
2. Bring up db+api (`make up`, or `docker run` equivalents until Compose is installed).
3. `make sync-github REPO=owner/name` ; `make sync-slack CHANNEL=C0123…` — watch
   `curl localhost:8000/health/db` node/edge counts climb.
4. Point the MCP client at this DB and ask the headline question:
   *"Which Slack threads discuss the feature that caused the deploy failure in <repo>?"*

**Goal C — release plumbing (optional):** `git init` + push; run the `gh api`
commands in `docs/BRANCH_PROTECTION.md` to require `lint`+`test`; add a `LICENSE`
(README says Apache-2.0); install the Compose plugin on the dev host.

## 11. Open follow-ups (parked, non-blocking)
- Live smoke for Teams/Zoom once creds exist; finish `gmeet` transcript retrieval.
- Wire `meeting`/`participant` extraction from transcripts (ontology terms exist).
- Cross-source user identity stitching (GitHub `alice` ≠ Slack `alice` today).
- JWKS signature verification in `IdentityResolver._decode` for production RBAC.
- For horizontal scaling, move `alembic upgrade head` out of the API lifespan into a
  one-shot `migrate` job (single-worker lifespan migration is fine today).
