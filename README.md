```
██╗      ██████╗ ██████╗ ███████╗    
██║     ██╔═══██╗██╔══██╗██╔════╝    
██║     ██║   ██║██████╔╝█████╗█████╗
██║     ██║   ██║██╔══██╗██╔══╝╚════╝
███████╗╚██████╔╝██║  ██║███████╗    
╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝    
██╗  ██╗███████╗███████╗██████╗ ███████╗██████╗ 
██║ ██╔╝██╔════╝██╔════╝██╔══██╗██╔════╝██╔══██╗
█████╔╝ █████╗  █████╗  ██████╔╝█████╗  ██████╔╝
██╔═██╗ ██╔══╝  ██╔══╝  ██╔═══╝ ██╔══╝  ██╔══██╗
██║  ██╗███████╗███████╗██║     ███████╗██║  ██║
╚═╝  ╚═╝╚══════╝╚══════╝╚═╝     ╚══════╝╚═╝  ╚═╝
                        a k r v s
```

> Ingest. Resolve. Cite. Repeat. A self-maintaining company brain for AI
> agents: connectors drain your fragmented tools into an ontology-backed
> knowledge graph, maintenance agents keep it deduped and fresh, and the whole
> thing is served over MCP — every answer traceable to the artifact that
> proves it. Ontologies die because a human has to babysit them. This one
> grooms itself and asks permission before it changes.

![status](https://img.shields.io/badge/status-ACTIVE-brightgreen)
![category](https://img.shields.io/badge/category-Knowledge%20Graph%20%2F%20MCP-9cf)
![difficulty](https://img.shields.io/badge/difficulty-Hard-red)
![python](https://img.shields.io/badge/python-3.12-blue)
![db](https://img.shields.io/badge/postgres-16%20%2B%20pgvector-blue)
![tests](https://img.shields.io/badge/tests-68%20passing-brightgreen)
![license](https://img.shields.io/badge/license-Apache--2.0-green)

```
┌─[ TARGET ]──────────────────────────────────────────────────────┐
│ codename   : lorekeeper                                         │
│ category   : Organizational Memory / Knowledge Graph            │
│ stack      : Python · FastAPI · SQLAlchemy · Postgres+pgvector  │
│ interfaces : MCP (stdio) · REST · CLI                           │
│ flags      : user [query the graph]  root [graph grooms itself] │
│ status     : ACTIVE — 7 connectors · 8 MCP tools · 68 tests     │
└─────────────────────────────────────────────────────────────────┘
```

## [ Briefing ]

LLM agents answering company questions hallucinate because company knowledge
lives in relationships, not paragraphs. Vector RAG retrieves the *most similar*
chunk; it has no idea that a **pull request** *implements* a **feature** that
*caused* an **incident** that a **Slack thread** *discusses*. Lorekeeper models
those edges explicitly, so an agent traverses from a repo to the humans arguing
about its outage — and cites the artifact behind every hop.

| | Vector RAG | Lorekeeper |
|---|---|---|
| Unit of knowledge | Text chunk | Typed **entity** + typed **relationship** |
| Cross-source links | None | Entities **deduplicated & merged** across sources |
| "How is A connected to B?" | Not answerable | **Multi-hop graph traversal** |
| Provenance | Chunk → document | Every node/edge → source artifact + URL |
| Freshness | Re-embed and pray | **Maintenance agents propose, you approve** |

Semantic search (pgvector) is still in the kit — you get both.

## [ Recon ] — the machine

```
  Connectors              Ontology Engine              Knowledge Graph             Agents
  (async httpx)          (LLM structured out)         (Postgres + pgvector)        (MCP)
┌────────────┐  raw    ┌────────────────────────┐    ┌──────────────────────┐    ┌──────────┐
│ GitHub     │ ──────▶ │ extract entities/edges │──▶ │ nodes   (+ embedding)│ ◀─ │ MCP      │ ◀─ Cursor /
│ Slack      │  docs   │ embed + deduplicate    │    │ edges   (typed)      │    │ server   │    Claude /
│ Notion…    │ ──────▶ │ map onto the ontology  │──▶ │ raw_documents        │    │ (stdio)  │    Windsurf
└────────────┘         └────────────────────────┘    │ node_mentions        │    └──────────┘
       │                          │                  │ proposals            │         │
       └─ raw_documents ──────────┴─ provenance ─────┴────── cite sources ────────────┘
```

A **property graph inside PostgreSQL** — relational rigor, ACID, `pgvector`
ANN search, and a clean migration path to a graph DB later:

| Table | Purpose |
|---|---|
| `ontology_node_types` / `ontology_relationship_types` | The ontology **is data** — extending it is an `INSERT`, not a migration |
| `raw_documents` | Immutable landing zone for every ingested artifact (payload + URL) |
| `ingestion_runs` | Connector run bookkeeping (status, cursor, stats) |
| `nodes` / `edges` | Entities (JSONB props + `vector(1536)`) and typed relationships |
| `node_mentions` | Provenance bridge: which documents a node was extracted from |
| `proposals` | The self-maintenance queue — every change the graph wants to make to itself |

Three rules keep it honest:

1. **One `nodes` table, not table-per-type** — uniform traversal, one HNSW index.
2. **The LLM can't invent types** — its structured-output enums are generated
   from the registry the database enforces.
3. **Nothing without receipts** — nodes carry mentions, edges carry evidence
   documents; the graph never asserts what it can't point at.

## [ Foothold ] — 30 seconds, zero keys

```bash
make demo
```

Spins a `pgvector` DB, ingests the bundled `demo_vault/` with the **offline
stub extractor** (deterministic embeddings, rule-based extraction — no API
spend), and prints the resolved graph: every `[[wikilink]]` a real edge. Tear
down with `make demo-down`.

The full stack:

```bash
cp .env.example .env             # LLM_PROVIDER=azure | anthropic | stub (offline)
make up                          # db + api; schema auto-migrates on startup
curl localhost:8000/health/db    # graph + ontology counts

make sync-github REPO=owner/name         # live ingest
make sync-slack  CHANNEL=C0123456789
make mcp                                  # the MCP server an agent launches
```

Register in Claude Desktop / Cursor: see [`docs/MCP_SETUP.md`](docs/MCP_SETUP.md).
Production: `docker compose -f docker-compose.prod.yml up --build -d` (DB port
unexposed, non-root image, migrations on boot).

## [ User Flag ] — query the graph

Four MCP tools, structured text out, UUIDs for chaining:

| Tool | What it does |
|---|---|
| `semantic_search(query, node_type?, limit?)` | pgvector ANN over all entities → ranked hits |
| `get_node_details(node_id)` | Properties + the **source documents/URLs** behind a node |
| `get_node_neighbors(node_id, direction?)` | 1-hop edges (incoming/outgoing/both) |
| `traverse_graph_path(start, target_type, max_depth=3)` | Multi-hop paths via bounded recursive CTE |

The agent flow: `semantic_search` → `traverse_graph_path` → `get_node_details`
→ cite. Ask it: *"Which Slack threads discuss the feature that caused the
deploy failure in this repo?"* — and get receipts.

Four more tools — `review_proposals`, `approve_proposal`, `reject_proposal`,
`rollback_proposal` — expose the maintenance queue, so you can groom the graph
without leaving your MCP client. That's the root flag:

## [ Root Flag ] — the graph grooms itself

The reason ontologies die: ~1 FTE per 50–100 entity types just to fight drift.
Lorekeeper's answer is the **proposal queue** — a single mutation choke point
for self-maintenance. Agents never edit the graph directly; they file typed,
evidence-backed proposals:

```
maintenance agents ──▶ proposals (pending) ──▶ human approves ──▶ applied
      dedup                 │    confidence ≥ threshold ──▶ auto-applied
      drift                 │                                    │
      staleness             └──▶ rejected (sticky — never       rollback
                                 re-filed for the same change)  (one command)
```

- **`entity_merge`** — fold duplicate entities: edges repointed, colliding
  facts folded (weights absorbed), mentions deduplicated, the loser kept as a
  soft alias so future mentions still resolve. Every apply captures a row-level
  snapshot; `rollback` restores the exact prior graph.
- **Auto-apply is OFF by default** (`PROPOSAL_AUTO_APPLY_THRESHOLD`) — trust is
  earned by watching the queue, then loosening. Auto-applied changes still get
  a proposal row: audited, reversible.
- Rejections are **sticky**: the dedup key stops agents from re-filing a merge
  a human already said no to.
- **The dedup agent** hunts what the insert-time resolver structurally can't:
  cross-source duplicates (GitHub `alice` ≡ Slack `alice` — both sourced, never
  compared) and gray-band near-misses below the auto-merge thresholds. Each
  proposal carries the evidence (trigram similarity, cosine distance, mention
  counts) a reviewer needs.
- **Every new candidate pair is put to the LLM as a merge judge** — similarity
  scores catch candidates, they can't judge identity (`payments-service` vs
  `payments-db` score high and are different things). A confident *different*
  verdict keeps the pair out of your queue entirely; *same* raises the
  proposal's confidence; the one-line rationale lands in the evidence next to
  the scores. Already-filed pairs are never re-judged, the offline stub
  abstains for free, and a judge outage degrades to plain heuristic proposals
  (`DEDUP_LLM_JUDGE`, on by default).
- **The staleness agent** flags nodes whose newest evidence went quiet
  (`STALE_AFTER_DAYS`, default 180) — confidence grows with age. Applying
  stamps `stale`/`stale_since` into the node's properties, so every MCP answer
  carries the warning; the fact keeps its citations, it just stops pretending
  to be fresh.
- **Drift detection** — the extractor has an escape hatch: entities that fit
  no ontology term are reported as `unmapped_types` instead of being forced
  into a wrong type or silently dropped. The pipeline files them as
  `schema_node_type` / `schema_relationship_type` proposals; approving one is
  an `INSERT` into the registry, and because the extraction prompt AND its
  structured-output enums are built from the **live registry**, the new term
  is extractable on the very next document. No restart, no migration.
- **Bootstrap** (`python -m app.bootstrap`) — right after connecting a new
  company's tools, sweep a sample of ingested documents and ask the LLM what
  vocabulary the ontology is missing wholesale. Same proposals, same queue,
  same human veto.

One shot of grooming, four ways to review:

```bash
make groom                                # run the agents, file proposals
make review-tui                           # split-pane TUI: one-key approve/reject
make review                               # the queue, highest confidence first
python -m app.review approve 0695fabc     # or reject / rollback / show
# ...or from Claude/Cursor: "review lorekeeper's pending proposals and apply
# the obvious ones"
```

The TUI (`python -m app.review tui`) is the fastest human loop: the queue on
top, the selected proposal's payload + evidence (scores, the judge's rationale,
rollback availability) below, and `a`/`r`/`b` to approve, reject, or roll back
— each with a y/N confirm. Stdlib curses, zero new dependencies.

<p align="center">
  <img src="assets/review-tui.gif" alt="the review TUI: approve a judge-confirmed merge, reject the pair the judge called different, then audit the trail" width="900">
</p>

> Above: the dedup agent filed two merges — the judge confirmed one (*same
> person seen in two tools*) and doubted the other (*a service and its
> database*). The human approves the first, rejects the second, then cycles
> the filter to see the full audit trail.

## [ Persistence ] — RBAC & posture

Graph-level RBAC is implemented and **defaults OFF**. Flip `RBAC_ENABLED=true`
and every MCP tool row-filters by the caller's grants: OIDC/JWT → principal →
groups → `access_grants` → visible `(source_system, resource_key)` set —
enforced in one place (`GraphRepository`), audited per call (`audit_log`).
Secrets live in the environment only; prod DB has no exposed port; the image
runs non-root; the MCP server logs strictly to stderr.

## [ Loadout ] — write a connector

Seven drivers ship: GitHub, Slack, Notion, Local/Obsidian (full), plus
Teams/Zoom/GMeet transcript scaffolds. Adding a source is one method:

```python
@ConnectorFactory.register("mysource")   # self-registers — no other file changes
class MySourceConnector(BaseConnector):
    source_system = "mysource"

    def fetch(self) -> Iterable[RawDoc]:
        ...  # yield RawDocs; retry/backoff, storage, extraction,
             # dedup, graph build, and MCP exposure are handled for you
```

Pipeline, `/ingest` API, and `--source` CLI discover it automatically.

## [ Ops ] — proof it works

The suite runs against a **real** pgvector database; connectors replay captured
API payloads through `httpx.MockTransport` (including a 429 to prove backoff).
CI gates every push: `ruff` (lint + format) → clean-DB migrations →
connector/integration/MCP checks → a live MCP **stdio handshake**.

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
python -m app.db.init_db && pytest tests/    # needs POSTGRES_*, LLM_PROVIDER=stub
```

```
app/
  config.py          # env-driven settings — nothing else reads os.environ
  pipeline.py        # connector → extract → embed → resolve; CLI entrypoint
  connectors/        # BaseConnector + 7 drivers + retry/backoff HTTP core
  llm/               # providers: Azure OpenAI · Anthropic (Claude) · offline stub
  ontology/          # extraction schema, embeddings, resolver (dedup)
  proposals/         # the self-maintenance engine: submit/approve/rollback
  agents/            # maintenance scanners (dedup, ...) — they only file proposals
  review.py          # the human side: CLI over the proposal queue
  review_tui.py      # ...and the curses TUI (python -m app.review tui)
  repositories/      # GraphRepository — THE read choke point
  security/          # principal, JWT identity, visibility, audit
  mcp/server.py      # FastMCP stdio server — the agent-facing tools
alembic/             # versioned migrations (applied on startup)
tests/               # unit + integration, run against live pgvector in CI
```

## [ License ]

Apache-2.0.
