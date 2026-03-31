# Developer Guide

Concise reference for contributors and developers extending Poly-QL.

---

## Dev Environment Setup

```bash
# 1. Clone & create virtualenv
git clone https://github.com/Mehul-Gupta-SMH/PolyQL.git
cd PolyQL
python -m venv venv && venv\Scripts\activate   # Windows
# source venv/bin/activate                      # macOS/Linux

# 2. Install all dependencies
pip install -r requirements.txt

# 3. Frontend
cd frontend && npm install && cd ..

# 4. Run tests (no API keys needed)
python -m pytest tests/ -v
```

**Start services:**
```bash
# Terminal 1 — backend (auto-reloads on file save)
venv/Scripts/uvicorn backend.app:app --reload --port 8000

# Terminal 2 — frontend (HMR)
cd frontend && npm run dev
```

---

## Architecture

```
Browser (React)
    │  HTTP (Vite proxy → localhost:8000)
    ▼
FastAPI  backend/app.py
    │  Depends(get_current_user)  ← JWT via backend/auth.py
    ▼
main.py
    ├── gatherRequirements()   Phase 1: agentic loop — asks clarifying Qs
    │       └── CallLLMApi     up to 5 get_schema tool calls
    └── generateQuery()        Phase 2: builds prompt, calls LLM, validates output
            ├── _get_business_context()              Business Semantic Layer
            ├── SQLBuilderSupport.getBuildComponents()   RAG retrieval
            ├── PromptBuilder.build()                    prompt assembly
            └── CallLLMApi.CallService()                 LLM call
```

### Storage backends

| Store | Technology | Purpose |
|---|---|---|
| Table/column metadata | SQLite (`tableMetadata.db`) | Structured schema facts — table descriptions, column types, pipeline logic |
| Vector embeddings | ChromaDB | Semantic search over table descriptions and business glossary terms |
| JOIN graph | Kuzu (embedded graph DB) | FK relationship graph — shortest-path JOIN route queries |
| Auth / sessions | SQLite (`app.db`) | User accounts, JWT sessions |
| Query outcomes | SQLite + JSONL (`outcomes.db` / `outcomes.jsonl`) | Per-execution success/failure telemetry |
| Business glossary | SQLite (`tableMetadata.db`, `business_terms` table) + ChromaDB (`business_glossary` collection) | Business term CRUD + semantic search |

Storage paths are configured in `Utilities/retrieval_config.YAML` and `Utilities/database_config.YAML`.

### RAG retrieval pipeline (`SQLBuilderComponents.py`)

1. **Embed** query → ChromaDB cosine search → top-N candidate tables (scoped by `instance_name`)
2. **Adaptive re-retrieval (R1)** → if fewer than `min_direct_tables` found, LLM rewrites the search query and retries (up to `max_rounds`)
3. **Kuzu graph traversal** → shortest-path JOIN bridge tables between retrieved nodes
4. **Cross-encoder reranker** → `FlagReranker.compute_score()` scores table relevance (outputs raw logits, typically −10 to +3); threshold `-5.0` filters low-confidence tables
5. **BM25 column scoring** → keep only relevant columns per table (PK/FK always retained)
6. **Business context** → `GlossaryStore.get_business_context()` cosine-searches glossary for the query; matching terms injected as `## Business Definitions` block
7. **Format** → `PromptBuilder.format_schema()` renders markdown schema + business definitions block

> **Reranker threshold note:** `FlagReranker` outputs raw logits (not 0–1 probabilities). The threshold in `retrieval_config.YAML` must be negative (default `-5.0`). Setting it to `0.0` silently discards all retrieved tables.

### LLM response envelope

All LLM responses are normalised to this JSON shape:

```json
{ "type": "sql|code|clarify", "content": "..." }
```

`_parse_llm_json()` in `main.py` handles plain-text fallbacks gracefully.

---

## Key Files

| File | Role |
|---|---|
| `main.py` | `generateQuery`, `gatherRequirements`, `generate_pipeline_dict`, validators, `_get_business_context` |
| `backend/app.py` | FastAPI routes, Pydantic models, auth/session endpoints |
| `backend/auth.py` | PyJWT tokens, PBKDF2 passwords, Google OAuth2, SQLite user/session CRUD |
| `backend/glossary.py` | Business Semantic Layer REST API (CRUD + bulk import + lexical search) |
| `backend/ingestion.py` | `parse_pipeline()`, `get_source_schema()`, `store_table()` (SQLite + ChromaDB + Kuzu) |
| `APIManager/AllAPICaller.py` | HTTP + subprocess LLM client; `model` override param; SSE streaming |
| `APIManager/PromptBuilder.py` | Loads `.txt` prompt templates, injects variables, formats schema + business definitions |
| `APIManager/model_access_config.YAML` | API keys + default model per provider (**gitignored**) |
| `APIManager/APIHeads/*.json` | Per-provider request templates (`<<api_key>>`, `<<model>>` placeholders) |
| `SQLBuilderComponents.py` | Orchestrates the retrieval pipeline; uses `Relations(strgType="kuzu")` |
| `MetadataManager/GlossaryStore.py` | `add_term`, `get_term`, `update_term`, `delete_term`, `list_terms`, `index_term`, `get_business_context` |
| `MetadataManager/MetadataStore/relationdb/kuzuDB.py` | Kuzu embedded graph; `_get_conn`, `_merge_node`, `_ensure_schema`, `addRelation`, `getRelation` |
| `Utilities/config.yaml` | Top-level config; paths to all sub-configs |
| `Utilities/retrieval_config.YAML` | ChromaDB, reranker, BM25, R1 re-retrieval, gather loop settings |
| `frontend/src/constants/providerLabels.js` | `PROVIDER_LABELS`, `PROVIDER_MODELS`, `defaultModel()` |
| `frontend/src/utils/api.js` | `apiFetch` — injects JWT header, fires `auth:logout` on 401 |
| `frontend/src/contexts/AuthContext.jsx` | Auth state, `login()`, `logout()`, Google SSO hash handling |
| `tests/conftest.py` | Mocks heavy ML deps; overrides `get_current_user` for test client |

---

## Kuzu Graph DB

Kuzu replaces the old NetworkX pickle as the JOIN-path store. Each `instance_name` gets its own Kuzu database file:

```
MetadataManager/MetadataStore/MetadataStorage/relationsdb/kuzudb/<instance_name>
```

### Schema

```
Node: KTable  { name: STRING }
Edge: JoinRel { JoinKeys: STRING }  (KTable → KTable)
```

### Key operations

```python
from MetadataManager.MetadataStore.relationdb import kuzuDB

conn = kuzuDB._get_conn("my_instance")
kuzuDB._ensure_schema(conn, "my_instance")

# Register a table node (idempotent MERGE)
kuzuDB._merge_node(conn, "orders")

# Add FK edge
kuzuDB.addRelation("my_instance", [
    ["orders", "customers", "orders.customer_id = customers.customer_id"]
])

# Find shortest JOIN path
path = kuzuDB.getRelation("my_instance", "orders", "products")
```

> **Important:** Every table must be registered as a Kuzu node via `_merge_node()` even if it has no FK edges — otherwise `getRelation()` raises `NodeNotFound`. `store_table()` in `backend/ingestion.py` always calls `_merge_node()` before processing relationships.

### Migration

On first run with an existing `Relations.pickle`, Kuzu auto-migrates: `kuzuDB._get_conn()` detects the pickle and imports all edges.

---

## Business Semantic Layer

### Architecture

```
POST /api/glossary/terms/single  →  GlossaryStore.add_term()
                                         ├── SQLite: business_terms table
                                         └── ChromaDB: business_glossary collection
                                               (embeds term_name + full_name + definition + synonyms)

generateQuery() / generateQueryStream() / gatherRequirements()
    └── main._get_business_context(query)
            └── GlossaryStore.get_business_context(query, top_k=3, distance_threshold=0.5)
                    ├── ChromaDB cosine search → top-k term IDs
                    └── SQLite enrichment → full term data
                            → [{ term_name, full_name, formula, table_deps, example_value }]

PromptBuilder.format_schema()
    └── if context['glossary_hits']:
            prepend ## Business Definitions block to schema section
```

### REST API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/glossary/terms/single` | Add one term |
| `POST` | `/api/glossary/terms/bulk` | Add multiple terms (JSON array) |
| `GET` | `/api/glossary/terms` | List all terms |
| `GET` | `/api/glossary/terms/{id}` | Get term by ID |
| `PUT` | `/api/glossary/terms/{id}` | Update term |
| `DELETE` | `/api/glossary/terms/{id}` | Delete term |
| `GET` | `/api/glossary/search?q=` | Lexical search by name |

### Term schema

```json
{
  "term_name": "ARR",
  "full_name": "Annual Recurring Revenue",
  "definition": "Sum of all recurring subscription revenue normalised to 12 months.",
  "formula": "SUM(monthly_revenue * 12) WHERE subscription_type = 'recurring'",
  "table_deps": ["subscriptions", "invoices"],
  "column_deps": ["monthly_revenue", "subscription_type"],
  "synonyms": ["annual revenue", "yearly revenue"],
  "example_value": "4200000"
}
```

---

## Benchmark Harness

The `benchmark/` module measures Poly-QL accuracy against BIRD and Spider text-to-SQL benchmarks.

### 3-step workflow

```
1. Ingest    →  benchmark/ingest_bird.py or ingest_spider.py or ingest_agent.py
                Creates a PolyQL instance per DB (SQLite + ChromaDB + Kuzu)

2. Infer     →  benchmark/run_inference.py
                Calls generateQuery() for each question; writes JSONL
                Resume-safe: skips question_ids with result_type=sql; retries errors

3. Evaluate  →  benchmark/evaluate.py
                Executes gold + predicted SQL against benchmark SQLite files
                Outputs EX (Execution Accuracy) and VES (Valid SQL rate)
                Breakdowns by difficulty (simple/moderate/challenging) and db_id
```

### Metrics

| Metric | Definition |
|---|---|
| **EX** | Execution Accuracy — predicted SQL returns the same result set as gold SQL (frozenset-of-frozensets comparison, order-insensitive) |
| **VES** | Valid SQL rate — fraction of predictions that execute without error |

### BIRD evidence injection

BIRD questions include an `evidence` field with domain knowledge hints. This is injected as an assistant exchange so ChromaDB RAG uses the plain question:

```python
# in run_inference.py  _build_conversation()
messages = [
    {"role": "user",      "content": f"Domain knowledge hint: {evidence}"},
    {"role": "assistant", "content": "Understood. I will apply that domain knowledge when writing the SQL."},
    {"role": "user",      "content": question},   # ← clean question for RAG
]
```

### Universal ingestion agent (`benchmark/ingest_agent.py`)

For benchmarks in formats other than BIRD/Spider, the LLM-powered ingestion agent auto-detects and handles any schema format:

```python
# Tools available to the agent
list_directory(path)       # explore the benchmark directory
read_file(path)            # inspect schema files (JSON, SQL, CSV, YAML)
store_table(...)           # write table to PolyQL metadata
clear_instance(name)       # wipe instance before force re-ingest
```

The agent runs a manual agentic loop with `claude-opus-4-6` until all tables are ingested. Requires `ANTHROPIC_API_KEY`.

### Known ingestion issues

- **CSV encoding**: Some BIRD CSV files are Windows-1252 encoded. `ingest_bird.py` tries `utf-8-sig` then falls back to `latin-1` automatically.
- **Composite PKs**: BIRD `primary_keys` can contain nested lists (`[[19, 20]]`). Both ingesters flatten them before building the PK set.
- **Isolated tables**: Tables with no FK declarations must still be registered as Kuzu nodes. `store_table()` always calls `_merge_node()` unconditionally.
- **Kuzu instance isolation**: Each benchmark DB instance has its own Kuzu file. Never share a Kuzu file across instances — it causes node contamination. Use `--force` to cleanly re-ingest.

---

## Adding a New LLM Provider

1. **Create a request template** `APIManager/APIHeads/MY_PROVIDER.json`:

```json
{
  "endpoint": "https://api.myprovider.com/v1/chat",
  "headers": { "Authorization": "Bearer <<api_key>>", "Content-Type": "application/json" },
  "payload": { "model": "<<model>>", "messages": [{"role": "user", "content": "<<input_text>>"}] }
}
```

2. **Add config** to `APIManager/model_access_config.YAML`:

```yaml
MY_PROVIDER:
  api_key: my-api-key
  model_name: my-default-model
  api_template: APIManager/APIHeads/MY_PROVIDER.json
```

3. **Wire up content injection and response parsing** in `AllAPICaller.CallService()`:

```python
# Content injection (after the existing open_ai / google blocks)
if self.llmService.lower() == "my_provider":
    self.api_temp_dict["payload"]["messages"][0]["content"] = prompt

# Response parsing
if self.llmService.lower() == "my_provider":
    return data["choices"][0]["message"]["content"]
```

4. **Register the provider** in `main.py`:

```python
_VALID_PROVIDERS = {"open_ai", "anthropic", "google", "groq", "codex", "claude_code", "my_provider"}
```

5. **Add frontend label and models** in `frontend/src/constants/providerLabels.js`:

```js
export const PROVIDER_LABELS = { ..., my_provider: 'My Provider' }
export const PROVIDER_MODELS = {
  ...,
  my_provider: [
    { value: 'my-default-model', label: 'My Default' },
    { value: 'my-fast-model',    label: 'My Fast' },
  ],
}
```

---

## Adding a New Prompt

1. Create `APIManager/Prompts/my_prompt.txt` with `<<VARIABLE>>` placeholders:

```
You are a data analyst.

Schema:
<<SCHEMA>>

Question: <<QUESTION>>

Return only SQL.
```

2. Register it in `PromptBuilder.__init__` (or the prompt map dict) — check `PromptBuilder.py` for the exact pattern used.

3. Call it:

```python
prompt = PromptBuilder('my_prompt').build({'SCHEMA': schema_str, 'QUESTION': user_q})
```

---

## Auth System

- **Tokens:** PyJWT HS256, 7-day expiry, signed with `JWT_SECRET` env var (random secret if unset — tokens invalidated on restart)
- **Passwords:** PBKDF2-SHA256, 260 000 iterations, random 16-byte salt
- **Storage:** SQLite at `backend/data/app.db` — tables `users` and `sessions`
- **FastAPI dependency:** `get_current_user` extracts and verifies the Bearer token; injected into every protected endpoint
- **Google SSO flow:**
  `GET /auth/google` → redirect to Google → `GET /auth/google/callback?code=...` → verify ID token → upsert user → issue JWT → redirect to `FRONTEND_URL/#sso_token=<jwt>`
  `AuthContext.jsx` reads the hash on mount and stores the token

**Environment variables for Google SSO:**

| Variable | Default |
|---|---|
| `GOOGLE_CLIENT_ID` | _(required)_ |
| `GOOGLE_CLIENT_SECRET` | _(required)_ |
| `GOOGLE_REDIRECT_URI` | `http://localhost:8000/auth/google/callback` |
| `FRONTEND_URL` | `http://localhost:5173` |
| `JWT_SECRET` | random (tokens expire on restart) |

---

## API Endpoints

### Public
| Method | Path | Description |
|---|---|---|
| `POST` | `/auth/register` | Create account `{username, password}` |
| `POST` | `/auth/login` | Get JWT `{username, password}` |
| `GET` | `/auth/google` | Start Google SSO flow |
| `GET` | `/auth/google/callback` | Google OAuth2 callback |
| `GET` | `/auth/google/enabled` | `{enabled: bool}` — whether Google SSO is configured |
| `GET` | `/api/providers` | List available providers |
| `GET` | `/api/providers/balance` | Per-provider balance / availability |
| `GET` | `/api/schema` | Full schema (tables + relations) |
| `GET` | `/api/lineage/{table}` | Lineage subgraph for a table |
| `GET` | `/metrics` | Prometheus metrics (request counts, latency histograms, LLM call counts) |

### Protected (Bearer token required)
| Method | Path | Description |
|---|---|---|
| `GET` | `/auth/me` | Current user info |
| `POST` | `/api/chat` | Two-phase chat: gather requirements → generate query |
| `POST` | `/api/chat/stream` | SSE streaming version of `/api/chat` |
| `POST` | `/api/query` | Direct query generation (no requirement gathering) |
| `POST` | `/api/execute` | Run SQL against a live database; records outcome to `outcomes.jsonl` |
| `POST` | `/api/ingest/preview` | Analyse pipeline SQL, return data dictionary preview |
| `POST` | `/api/ingest/commit` | Save reviewed table metadata to schema stores |
| `GET` | `/api/instances` | List all named DB instances |
| `GET` | `/api/joinpath` | Shortest JOIN path between two tables |
| `GET` | `/api/derivatives/{table}` | Parent/child tables for a derived table |
| `GET` | `/api/sessions` | List user's saved sessions |
| `POST` | `/api/sessions` | Upsert a session |
| `DELETE` | `/api/sessions/{id}` | Delete a session |

### Glossary (Business Semantic Layer)
| Method | Path | Description |
|---|---|---|
| `POST` | `/api/glossary/terms/single` | Add one business term |
| `POST` | `/api/glossary/terms/bulk` | Bulk import terms (JSON array) |
| `GET` | `/api/glossary/terms` | List all terms |
| `GET` | `/api/glossary/terms/{id}` | Get term by ID |
| `PUT` | `/api/glossary/terms/{id}` | Update term |
| `DELETE` | `/api/glossary/terms/{id}` | Delete term |
| `GET` | `/api/glossary/search?q=` | Lexical search by term name |

---

## Testing

```bash
python -m pytest tests/ -v            # all 247 tests
python -m pytest tests/test_api.py   # API / integration tests only
python -m pytest tests/test_glossary_store.py tests/test_glossary_retrieval.py  # Semantic Layer
python -m pytest tests/test_ingestion.py       # pipeline SQL parsing
python -m pytest tests/test_kuzu.py            # graph DB
python -m pytest tests/test_failure_scenarios.py  # LLM failure modes
```

**How tests work without ML models:**
- `tests/conftest.py` stubs out `torch`, `chromadb`, `sentence_transformers`, `FlagEmbedding` before any imports
- `get_current_user` FastAPI dependency is overridden to return a fake user, so protected endpoints work without a JWT
- CI uses `requirements-ci.txt` (no heavy packages)

**Adding tests:** follow the existing pattern — mock `CallLLMApi` or `generateQuery` at import location (`backend.app.generateQuery`), not the source module.

**Test counts by area:**
| Area | Tests |
|---|---|
| Core API & integration | ~70 |
| Retrieval pipeline & adaptive re-retrieval | ~25 |
| Ingestion (pipeline SQL parsing, C4 qualified names) | ~24 |
| Kuzu graph DB | ~15 |
| Business Semantic Layer (CRUD + retrieval) | ~54 |
| Failure scenarios (LLM timeout/429/malformed) | ~16 |
| Observability (metrics, logging) | ~23 |
| Other utilities | ~20 |
| **Total** | **247** |

---

## Observability

- **JSON logs** — `configure_logging()` in `backend/logging_config.py` switches the root logger to single-line JSON. Called once at FastAPI lifespan startup. Every HTTP request is logged with `method`, `path`, `status`, `latency_ms`.
- **Prometheus metrics** — `GET /metrics` returns valid Prometheus text format. In-memory only (no external dependency). Tracks:
  - `http_requests_total{method, path, status}`
  - `http_request_duration_seconds{method, path}` (histogram)
  - `llm_calls_total{provider}`
  - `llm_errors_total{provider}`

---

## Frontend Overview

```
frontend/src/
├── App.jsx                    # Tabs: Query | Schema/ERD | Ingest Table | Data Lineage
├── contexts/
│   └── AuthContext.jsx        # user, token, login(), logout(), ssoError
├── utils/
│   └── api.js                 # apiFetch — auto-injects Authorization header
├── constants/
│   └── providerLabels.js      # PROVIDER_LABELS, PROVIDER_MODELS, defaultModel()
└── components/
    ├── LoginPage.jsx           # Sign-in / register form + Google SSO button
    ├── ChatInterface.jsx       # Main chat UI, session pane, toolbar, SSE streaming
    ├── ChatMessage.jsx         # Message bubbles, RunQueryPanel, outcome badge
    ├── SchemaERD.jsx           # React Flow ERD diagram
    ├── IngestTable.jsx         # Pipeline SQL ingestion wizard
    └── DataLineage.jsx         # Lineage graph viewer
```

**State flow for a chat turn:**

```
handleSend()
  → apiFetch POST /api/chat/stream  { messages, provider, query_type, model }
  → gatherRequirements()            Phase 1 — may return { type: "clarify" }
  → generateQueryStream()           Phase 2 — SSE token stream
  → pushMsg()                       adds message bubble to state
  → useEffect[messages]             auto-saves session to server
```

**Outcome badge:** After every SQL execution (`POST /api/execute`), the UI updates an inline badge — green ✓ (success), amber ○ (empty result), red ✕ (error) — and a coloured dot on the session pane entry.

---

## Git Workflow

- `master` — stable, deployed branch
- `Claude/Playground/Dev` — general development branch
- `Claude/feature/<name>` — feature branches for larger pieces of work
- Never push directly to `master`; open a PR from the feature branch
- CI runs `ruff check backend/ tests/ validation/` then `pytest tests/` on every PR to `master`
