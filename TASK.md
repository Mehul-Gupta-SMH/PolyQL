# Poly-QL — Project Task Reference

## What is this project?
**Poly-QL** (formerly SQLCoder) is a full-stack AI-powered data query and metadata management tool.
Users describe questions in natural language; a two-agent system gathers requirements then generates SQL / Spark SQL / PySpark DataFrame code. It also supports ingesting new pipeline tables and browsing data lineage.

---

## Architecture

```
frontend/ (Vite + React)          backend/ (FastAPI)            Storage
──────────────────────            ──────────────────            ───────
ChatInterface                     POST /api/chat                ChromaDB (vector embeddings)
  └─ SessionPane (history)          └─ gatherRequirements()     NetworkX graph (Relations.pickle)
  └─ ChatMessage                        └─ tool loop: get_schema SQLite (tableMetadata.db)
SchemaERD                               └─ generateQuery()
  └─ TableNode                    POST /api/query (legacy)
IngestTable                       POST /api/ingest/preview
JoinPathExplorer                  POST /api/ingest/commit
  └─ Join Path tab                POST /api/execute
  └─ Derivative Tables tab        GET  /api/schema?instance_name=
                                  GET  /api/joinpath?from_table=&to_table=
                                  GET  /api/derivatives/{table}
                                  GET  /api/instances
                                  GET  /api/lineage/{table}  (kept for compat)
                                  GET  /api/providers
                                  GET  /api/providers/balance
```

---

## Key Files

| File | Role |
|------|------|
| `main.py` | `generateQuery()`, `gatherRequirements()`, `generate_pipeline_dict()`, `validate_sql()`, `validate_pyspark()` |
| `backend/app.py` | FastAPI routes |
| `backend/ingestion.py` | `parse_pipeline()`, `get_source_schema()`, `store_table()` |
| `backend/executor.py` | SQLAlchemy query execution (500 row cap) |
| `backend/balance.py` | Provider credit / availability checks (concurrent) |
| `SQLBuilderComponents.py` | 3-step RAG retrieval (ChromaDB → NetworkX → SQLite) |
| `APIManager/PromptBuilder.py` | Maps prompt type strings to `.txt` template files |
| `APIManager/AllAPICaller.py` | Multi-provider LLM caller (OpenAI, Anthropic, Google, GROQ, Codex, Claude Code CLI) |
| `MetadataManager/MetadataStore/RAGPipeline.py` | Embedding + reranking pipeline |
| `MetadataManager/MetadataStore/ManageRelations.py` | NetworkX graph wrapper |
| `MetadataManager/MetadataStore/relationdb/networkxDB.py` | Graph load/save/query |
| `MetadataManager/MetadataBuilder/importExisting/importData.py` | `importDD` — SQLite metadata writer |
| `Utilities/base_utils.py` | `accessDB` (SQLite CRUD), `get_config_val` (YAML config) |

---

## Prompt Templates (`APIManager/Prompts/`)

| File | Prompt Type | Params | Returns |
|------|-------------|--------|---------|
| `taskRequirementGather.txt` | `gather requirements` | `TABLE_DIRECTORY, SCHEMA, FETCHED_SCHEMAS, CONVERSATION` | JSON `{action: get_schema, table}` or `{ready, question/summary}` |
| `taskGenerateSQL.txt` | `generate sql` | `CONVERSATION, SCHEMA` | JSON `{type: sql\|clarify, content}` |
| `taskGenerateSparkSQL.txt` | `generate spark sql` | `CONVERSATION, SCHEMA` | JSON `{type: sql\|clarify, content}` |
| `taskGenerateDataframeAPI.txt` | `generate dataframe api` | `CONVERSATION, SCHEMA` | JSON `{type: code\|clarify, content}` |
| `taskIngestPipeline.txt` | `ingest pipeline` | `SQL, SOURCE_SCHEMAS, COLUMN_MAPPINGS` | JSON `{tableDesc, columns[]}` |
| `taskExtractRelations.txt` | `extract relations` | `SQLQuery` | Relations list |
| `taskGenerateTableSummary.txt` | `create data dict` | — | Table summary |
| `taskGenerateColumnDesc.txt` | `create table summary` | — | Column descriptions |

---

## Storage Locations (from `Utilities/retrieval_config.YAML`)

| Store | Path |
|-------|------|
| SQLite metadata | `MetadataManager/MetadataStore/MetadataStorage/db/table/tableMetadata.db` |
| ChromaDB | `MetadataManager/MetadataStore/MetadataStorage/vdb/` |
| NetworkX pickle | `MetadataManager/MetadataStore/MetadataStorage/relationsdb/Relations.pickle` |
| Config root | `Utilities/config.yaml` → `database_config.YAML`, `retrieval_config.YAML`, `model_config.YAML` |

---

## Two-Agent Query Flow

```
User sends message (with full conversation history)
  │
  POST /api/chat  {messages, provider, query_type}
  │
  ▼  Phase 1 — Requirement Gathering Agent  (gatherRequirements)
  ├─ Sees: table directory (all tables + descriptions)
  ├─ Sees: RAG schema (ChromaDB search on recent user messages)
  ├─ Sees: full conversation history
  │
  │  Tool loop (up to 5 calls):
  ├─ LLM → {"action": "get_schema", "table": "X"}
  │    └─ _get_full_table_schema("X") → SQLite fetch → appended to FETCHED_SCHEMAS
  ├─ LLM → {"action": "get_schema", "table": "Y"}  (repeat as needed)
  │
  ├─ LLM → {"ready": false, "question": "I found table X with column Y — what threshold?"}
  │    └─ Returned to user as clarify bubble
  │
  └─ LLM → {"ready": true, "summary": "...complete requirements..."}
       │
       ▼  Phase 2 — Query Generation Agent  (generateQuery)
       ├─ Uses summary as search query for targeted schema fetch
       ├─ Sees: full conversation (<<CONVERSATION>>) + schema (<<SCHEMA>>)
       └─ Returns SQL / Spark SQL / PySpark code
```

---

## Ingest Pipeline Flow

```
User pastes INSERT…SELECT or CREATE TABLE AS SELECT
  │
  POST /api/ingest/preview
  ├─ parse_pipeline()        → target table, column mappings, source tables
  ├─ get_source_schema()     → fetch source column metadata from SQLite
  ├─ format_source_schemas() → format for LLM
  └─ generate_pipeline_dict() → LLM generates target table data dictionary
  │
  User reviews + edits descriptions + confirms relationships
  │
  POST /api/ingest/commit
  └─ store_table()
       ├─ SQLite: tableDesc + tableColMetadata (source_expr stored in `logic` column)
       ├─ ChromaDB: table description embedding
       └─ NetworkX: source_table → target_table edges
```

---

## Provider Configuration (`APIManager/model_access_config.YAML`)

| Key | Model | Auth |
|-----|-------|------|
| `OPEN_AI` | gpt-3.5-turbo | API key → Bearer token |
| `ANTHROPIC` | claude-3-5-haiku-20241022 | API key → x-api-key · `/v1/messages` |
| `GROQ` | gemma-7b-it | API key → Bearer token |
| `GOOGLE` | gemini-2.0-flash | API key → query param (`?key=`) via `v1beta` |
| `CODEX` | o4-mini | API key → Bearer token (same format as OPEN_AI) |
| `CLAUDE_CODE` | claude-sonnet-4-5 | **CLI subprocess** — uses local `claude` session, no API key |

Balance checking (`GET /api/providers/balance`):
- OpenAI/Codex: tries `/v1/organization/credits` → returns dollar amount
- Anthropic/Claude Code: probes `/v1/models` (key validity) + 1-token POST (credit check)
- GROQ: probes `/openai/v1/models`
- Google: probes models endpoint, checks error status codes
- Claude Code: runs `claude --version` — shows CLI version or "CLI not installed"

---

## Frontend Tabs

| Tab | Component | Description |
|-----|-----------|-------------|
| Query | `ChatInterface` + `SessionPane` + `ChatMessage` | Chat UI with left-pane history. Sessions auto-saved to localStorage (max 30). |
| Schema / ERD | `SchemaERD` + `TableNode` | Interactive React Flow diagram; click table → side pane data dictionary |
| Ingest Table | `IngestTable` | 2-step wizard: paste pipeline SQL → review LLM-generated data dictionary → commit |
| Data Lineage | `DataLineage` | Select table → React Flow neighborhood graph showing connected tables |

---

## Running the App

```bash
# Backend (from project root)
uvicorn backend.app:app --reload

# Frontend (from frontend/)
npm run dev
```

Backend: http://localhost:8000
Frontend: http://localhost:5173

---

## Agent Workflow

| Role | Agent | Responsibilities |
|------|-------|-----------------|
| Planning | Claude + Codex | Review backlog, agree on scope and approach |
| Development | Claude Sonnet 4.6 | Implement features, fix bugs |
| Testing + Commits | OpenAI Codex (o4-mini) | Run tests, add missing tests, git commit with both as co-authors |

Handoff: Claude marks task `[~]` and notes "ready for Codex" → Codex tests → Codex commits → marks `[x]`

---

## Change Log

| Date | Branch | File | Change |
|------|--------|------|--------|
| 2026-03-31 | `Claude/feature/benchmark-harness` | `SQLBuilderComponents.py` | Fix: change Relations strgType from "networkx" to "kuzu" so retrieval uses the correct backend |
| 2026-03-31 | `Claude/feature/benchmark-harness` | `Utilities/retrieval_config.YAML` | Fix: reranker_threshold 0.0 → -5.0 (FlagReranker outputs negative logits; 0.0 silently dropped all results) |
| 2026-03-31 | `Claude/feature/benchmark-harness` | `benchmark/sample_data/` | Sample BIRD-format benchmark: music_store SQLite + tables.json + 8 questions (simple/moderate/challenging) |
| 2026-03-31 | `Claude/feature/benchmark-harness` | `benchmark/ingest_bird.py`, `ingest_spider.py` | Richer table descriptions with column details for better reranker scoring |
| 2026-03-31 | `Claude/feature/benchmark-harness` | `benchmark/run_inference.py` | Fix conversation builder: evidence as assistant exchange, plain question as final user turn for clean RAG |
| 2026-03-29 | `Claude/feature/benchmark-harness` | `benchmark/` | BM0–BM3: benchmark evaluation harness — `_bench_utils.py` (shared helpers), `ingest_bird.py`, `ingest_spider.py`, `run_inference.py`, `evaluate.py`; EX + VES metrics; per-difficulty/db breakdown; resume-safe JSONL output |
| 2026-03-11 | `Claude/Playground/Dev` | `TASK.md` | Marked A2 complete and recorded the pytest run + Kuzu test coverage in the log. |
| 2026-03-11 | `Claude/Playground/Dev` | `main.py` | P4: added `_preload_schemas_bulk()` — 2 SQLite queries upfront replace N×2 per-table queries in gather loop; `gatherRequirements` serves `get_schema` calls from cache |
| 2026-03-11 | `Claude/Playground/Dev` | `pyproject.toml` | New: proper project metadata, runtime deps, optional `dev` + `graph-db` extras, pytest config, ruff config |
| 2026-03-11 | `Claude/Playground/Dev` | `TASK.md` | Updated A2 to target Kuzu (embedded, no-server graph DB) instead of Neo4j; added Kuzu to `graph-db` optional extra |
| 2026-03-11 | `Claude/Playground/Dev` | `AGENTS.md`, `TASK.md` | Established two-agent workflow: Claude=developer, Codex=tester+git manager, both=planning |
| 2026-03-07 | `Claude/Playground/Dev` | `backend/balance.py` | Fixed OpenAI `credit_grants` URL: `/dashboard/billing/credit_grants` → `/v1/dashboard/billing/credit_grants`. Added key-validity fallback via `GET /v1/models` for secret keys that cannot access the billing endpoint (returns 403). Provider now correctly shows `N/A` (not greyed out) when key is valid but balance is inaccessible. |
| 2026-03-07 | `Claude/Playground/Dev` | `backend/usage_tracker.py` | New module. Thread-safe in-memory tracker for Claude Code CLI token usage (calls, input_tokens, output_tokens, cost_usd). Resets on backend restart. |
| 2026-03-07 | `Claude/Playground/Dev` | `APIManager/AllAPICaller.py` | Claude Code CLI: switched `--output-format text` → `--output-format json` to capture token usage per call. Records to `usage_tracker`. Falls back to raw stdout on JSON parse failure. |
| 2026-03-07 | `Claude/Playground/Dev` | `backend/balance.py` (`_check_claude_code`) | Label now shows session token usage after calls: `CLI 1.x.x · N↑ M↓ tok · $X.XXXX`. |
| 2026-03-07 | `Claude/Playground/Dev` | `main.py` (`gatherRequirements`) | Non-JSON response from LLM now triggers a one-shot re-prompt with strict JSON correction instruction before falling back. Extracts `options` array from clarify response and passes it through. |
| 2026-03-07 | `Claude/Playground/Dev` | `APIManager/Prompts/taskRequirementGather.txt` | Added `options` array to the clarify JSON format. Strengthened JSON-only enforcement with CRITICAL note. |
| 2026-03-07 | `Claude/Playground/Dev` | `backend/app.py` | `/api/chat` now passes `options` array in clarify responses when present. |
| 2026-03-07 | `Claude/Playground/Dev` | `frontend/src/components/ChatInterface.jsx` | Retry now sends only user `text` messages (strips assistant clarify history) so gathering agent re-evaluates fresh. Added `handleOptionSelect` to send clicked option as a user message. Passes `onOptionSelect` to `ChatMessage`. |
| 2026-03-07 | `Claude/Playground/Dev` | `frontend/src/components/ChatMessage.jsx` | Clarify bubble now renders clickable option buttons (`msg.options`) styled as pill buttons. Accepts `onOptionSelect` prop. |
| 2026-03-08 | `Claude/Playground/Dev` | `main.py` (`generateQuery`) | Fixed empty schema bug: RAG search now uses the original user conversation messages instead of the requirements summary. Summary text is too structured to match ChromaDB table-description embeddings, resulting in empty `## Database Schema`. Two-stage fallback: conversation → summary → log error. |
| 2026-03-08 | `Claude/Playground/Dev` | `APIManager/PromptBuilder.py` (`format_schema`) | Added fallback note when no tables are retrieved, so LLM gets an explicit signal instead of a blank schema section. |
| 2026-03-08 | `Claude/Playground/Dev` | `APIManager/AllAPICaller.py` (claude_code) | Fixed: Claude Code CLI was receiving the full task prompt via `-p` (user message), causing Claude to treat it as pasted content and respond conversationally. Now passes prompt via `--system-prompt` and sends a neutral activation trigger as `-p`. |
| 2026-03-08 | `Claude/Playground/Dev` | Multiple | Added Pandas as a query type: new prompt `taskGeneratePandas.txt`, `validate_pandas()` in `main.py`, registered in `PromptBuilder`, `_VALID_QUERY_TYPES`, `_PROMPT_MAP`, `app.py` Literal, frontend `QUERY_TYPE_LABELS` + `ChatMessage` label/notice. |
| 2026-03-13 | `Claude/Playground/Dev` | `main.py`, `Utilities/retrieval_config.YAML`, `tests/test_adaptive_retrieval.py` | R1: Adaptive re-retrieval agent. New `_adaptive_retrieval()` + `_is_retrieval_confident()` in `main.py`. Config-driven `max_rounds/min_direct_tables`. Replaces 2-line fallback in both `generateQuery` and `generateQueryStream`. 9 tests added, 117 total pass. → Codex: commit |
| 2026-03-20 | `Claude/feature/validation-outcome-layer` | `backend/ingestion.py`, `tests/test_ingestion.py` | C4 done: qualified name support (db.schema.table) — 3 regex patterns updated to capture dot-separated names; quoted forms (backtick/double-quote/bracket) handled via alternation; 24 new tests covering bare/2-part/3-part/quoted names for INSERT/CTAS targets and FROM/JOIN sources — 193 tests pass |
| 2026-03-20 | `Claude/feature/validation-outcome-layer` | `.github/workflows/ci.yml`, `requirements-ci.txt`, `backend/`, `tests/`, `validation/` | CI2 done: ruff lint step added to CI (scoped to backend/tests/validation); fixed 19 pre-existing violations in those dirs; lint runs before tests on every PR |
| 2026-03-20 | `Claude/feature/validation-outcome-layer` | `backend/logging_config.py`, `backend/metrics.py`, `backend/app.py`, `tests/test_metrics.py` | B3 done: JSON structured logging (JsonFormatter, configure_logging), in-memory Prometheus metrics (request counts/latency histograms/LLM call counters), GET /metrics endpoint, request-logging middleware, LLM call instrumentation in /api/chat and /api/chat/stream — 23 new tests |
| 2026-03-20 | `Claude/feature/validation-outcome-layer` | `tests/test_failure_scenarios.py` | T2 done: 16 failure-scenario tests covering LLM timeout, 429 rate-limit, malformed/empty LLM response, network ConnectionError, and /api/execute DB errors — all 169 tests pass |
| 2026-03-16 | `Claude/feature/validation-outcome-layer` | `TASK.md` | C4 added: database.schema.table naming gap — 5 breaking points documented, fix design recorded (separate db_name/schema_name columns + double-underscore Kuzu key) |
| 2026-03-16 | `Claude/feature/validation-outcome-layer` | `frontend/src/components/ChatMessage.jsx` | QT2 done: OutcomeBadge component — ✓ green/○ amber/✕ red inline with Execute button |
| 2026-03-16 | `Claude/feature/validation-outcome-layer` | `.github/workflows/ci.yml` | CI1 done: re-enabled pull_request trigger on master |
| 2026-03-15 | `Claude/feature/validation-outcome-layer` | `TASK.md` | CI1–CI8: CI/CD refinement tasks added to backlog |
| 2026-03-15 | `Claude/feature/validation-outcome-layer` | `validation/outcome_store.py`, `backend/app.py`, `tests/test_outcome_store.py`, `tests/conftest.py` | QT1 done: outcome recorder appends to outcomes.jsonl + SQLite index after every /api/execute; ExecuteRequest extended with optional nl_query/provider/session_id; 13 new tests, 130 total pass |
| 2026-03-15 | `Claude/feature/validation-outcome-layer` | `demo/record_pages.py` | New per-page demo recorder: 4 standalone clips (query chat, schema ERD, join path, ingest table) for LinkedIn showcase; replaces single-monolith record_demo.py |
| 2026-03-15 | `Claude/Playground/Dev` | `TASK.md` | QT1–QT4: Query Outcome Tracking feature added — runtime success/failure recorder, UI badge, corpus label feed, async failure classifier |
| 2026-03-15 | `Claude/Playground/Dev` | `TASK.md` | VL0–VL9: Validation Layer redesigned — history-anchored corpus builder (joins QT outcomes), contender generator, typed perturbation engine, baseline canary runner, runnability checker, join advisor, daily perf log, prompt enhancement feed, scheduling config, dashboard endpoints |
| 2026-03-13 | `Claude/Playground/Dev` | `APIManager/AllAPICaller.py`, `main.py`, `backend/app.py`, `frontend/…/ChatInterface.jsx`, `frontend/…/ChatMessage.jsx` | P1: Streaming LLM responses. Added `CallServiceStream()` generator (OpenAI/Codex/GROQ/Anthropic/Google SSE; CLI providers fall back to batch). Added `generateQueryStream()` in `main.py`. New `POST /api/chat/stream` SSE endpoint in FastAPI. Frontend uses `fetch` ReadableStream to display tokens incrementally in a `streaming` bubble, finalised by `done` event. → Codex: run tests, commit |

---

## Task Backlog

Tasks ordered by impact tier. Status: `[ ]` pending · `[~]` in progress · `[x]` done

### Tier 1 — Security

| ID | Status | Task | Files |
|----|--------|------|-------|
| S1 | [x] | Replace `eval()` with `json.loads()` for API template loading | `APIManager/AllAPICaller.py:68` |
| S2 | [x] | Replace `eval()` with `ast.literal_eval()` for cache deserialization | `Utilities/base_utils.py:308` |
| S3 | [x] | Verify `model_access_config.YAML` was never committed to git history | confirmed clean — no commits found |

### Tier 2 — High Impact, Low Complexity

| ID | Status | Task | Files |
|----|--------|------|-------|
| Q1 | [x] | Remove hardcoded absolute paths — move to env vars or relative paths | `Utilities/base_utils.py:33`, all `Utilities/*.YAML` |
| Q2 | [x] | Remove double `__set_apidict__()` call — called in both `__init__` and `CallService` | `APIManager/AllAPICaller.py` |
| Q3 | [x] | Fix `.gitignore` — replace `/__pycache__` with `**/__pycache__` to catch nested dirs | `.gitignore` |
| Q4 | [x] | Replace all `print()` debug statements with `logging` | `SQLBuilderComponents.py`, `MetadataBuilder/importExisting/importData.py` |

### Tier 3 — High Impact, High Complexity

| ID | Status | Task | Files |
|----|--------|------|-------|
| C1 | [x] | Build structured prompt template — format context as DDL/markdown, not raw dict repr | `main.py`, `APIManager/PromptBuilder.py`, `APIManager/Prompts/taskGenerateSQL.txt` |
| C2 | [x] | Wire reranker scores into actual filtering — threshold or top-k cut on scored results | `SQLBuilderComponents.py`, `Utilities/retrieval_config.YAML` |
| C3 | [x] | Implement `__filterAdditionalColumns__()` — filter columns by relevance to user query | `SQLBuilderComponents.py` |
| C4 | [x] | **`database.schema.table` fully-qualified name support** — current system only stores bare table names; qualified names (`db.schema.table`) break in five distinct places; add `database_name` and `schema_name` as separate optional columns to SQLite metadata tables (mirroring the existing `instance_name` pattern), use `{db}__{schema}__{table}` as Kuzu node key, and reassemble `database.schema.table` for display/prompt output | `Utilities/base_utils.py`, `MetadataManager/MetadataBuilder/importExisting/importData.py`, `backend/ingestion.py`, `MetadataManager/MetadataStore/relationdb/kuzuDB.py`, `main.py`, `SQLBuilderComponents.py` |

### Tier 4 — Medium Impact, Medium Complexity

| ID | Status | Task | Files |
|----|--------|------|-------|
| M1 | [x] | Switch `nx.Graph` to `nx.DiGraph` for directional JOIN relationships | `MetadataManager/MetadataStore/relationdb/networkxDB.py` |
| M2 | [x] | Fix silent error swallowing in `importData.py` — preserve original exception | `MetadataManager/MetadataBuilder/importExisting/importData.py` |
| M3 | [x] | Move module-level config/model loading in `RAGPipeline.py` into class `__init__` | `MetadataManager/MetadataStore/RAGPipeline.py` |
| M4 | [x] | Add retry/fallback logic to `CallLLMApi.CallService()` | `APIManager/AllAPICaller.py` |

### Tier 5 — Maintainability & Future-Proofing

| ID | Status | Task | Files |
|----|--------|------|-------|
| F1 | [x] | Introduce `VectorStore` interface to abstract ChromaDB — prep for Pinecone/QDrant | `MetadataManager/MetadataStore/vdb/base.py`, `vdb/Chroma.py`, `RAGPipeline.py` |
| F2 | [x] | Introduce metadata store interface to abstract SQLite — prep for other backends | `Utilities/store_interface.py`, `Utilities/base_utils.py` |
| F3 | [x] | Add a test suite | `tests/test_prompt_builder.py`, `tests/test_filters.py`, `tests/test_base_utils.py` |
| F4 | [x] | Fix `cachefunc.close()` — references `self.connection` which doesn't exist on the class | `Utilities/base_utils.py` |

---

---

## Codex Ideas Backlog
Generated by `o4-mini` on 2026-03-08. Status: `[ ]` pending · `[~]` in progress · `[x]` done

### Performance & Latency

| ID | Status | Idea | Complexity |
|----|--------|------|------------|
| P1 | [x] | **Streaming LLM responses** — implement token-by-token streaming in FastAPI (StreamingResponse) and show partial tokens in the chat bubble as they arrive | Medium |
| P2 | [ ] | **In-process RAG cache** — cache recent ChromaDB vector search results and NetworkX traversals (e.g. TTL dict) to skip repeated heavy retrievals for the same query | Medium |
| P3 | [ ] | **Async DB & I/O calls** — convert SQLite and ChromaDB interactions to async (aiosqlite / async ChromaDB client) to improve throughput under concurrent load | Low |
| P4 | [x] | **Batch schema fetching** — preload all tool-requested table schemas in one DB round-trip per gather cycle rather than one query per `get_schema` call | Low |
| P5 | [ ] | **Lazy-load ERD** — only fetch and render visible nodes in the Schema/ERD tab; virtualise large graphs (50+ tables) so initial load stays fast | Low |

### Architecture / Scalability

| ID | Status | Idea | Complexity |
|----|--------|------|------------|
| A1 | [ ] | **Migrate metadata to managed DB** — replace local SQLite with PostgreSQL/MySQL to support multi-instance writes, backups, and concurrent access | Medium |
| A2 | [x] | **Replace NetworkX pickle with Kuzu (embedded graph DB)** — Kuzu is SQLite-style (no server), has Python bindings + Cypher support, and replaces the pickle file with a real persistent graph store. Swap `networkxDB.py` → `kuzuDB.py` behind the existing `ManageRelations` interface; auto-migrate existing pickle on first run. Add `kuzu` to `pyproject.toml` `graph-db` extra. | Medium |
| A3 | [x] | **Configurable tool-loop cap** — externalise `gatherRequirements` iteration limit (currently hard-coded 5) and per-call timeout to `config.yaml` | Low |
| A4 | [ ] | **API rate limiting & request queuing** — add a token-bucket rate-limiter at the FastAPI layer (e.g. `slowapi`) to protect downstream LLM services | Medium |
| A5 | [ ] | **Authentication & multi-tenancy** — add OAuth2 / JWT auth and isolate per-user sessions and metadata | High |
| R1 | [x] | **Adaptive re-retrieval agent** — when initial ChromaDB/RAG retrieval returns weak or no table matches, run an iterative re-retrieval loop: LLM rewrites the search query using available table names + prior results, re-queries ChromaDB, and repeats until a confidence threshold is met or a max-retry cap is hit. Prevents silent empty-schema failures in `generateQuery`. → Codex: plan approach, files to touch, edge cases, tests needed | High |

### UX / Frontend

| ID | Status | Idea | Complexity |
|----|--------|------|------------|
| U1 | [ ] | **ERD search/filter** — add a search bar and tag-filter on the Schema/ERD tab to locate tables quickly in large schemas | Low |
| U2 | [ ] | **Streaming chat UI** — show SQL/code tokens incrementally with a "Stop" button; pairs with P1 | Medium |
| U3 | [ ] | **Session export/import** — allow exporting a session as JSON or Markdown and re-importing it, bypassing the localStorage 5 MB limit | Low |
| U4 | [ ] | **Ingest diff view** — side-by-side before/after diff of original vs LLM-suggested data dictionary fields during ingestion review | Medium |
| U5 | [ ] | **Mobile responsiveness** — ensure chat and schema views adapt to small screens | Low |
| U6 | [ ] | **ERD edge types** — render relationship arrows with directionality annotations (1:1, 1:n, n:1, n:m) instead of flat arrows in the Schema/ERD view | Medium |

### Backend Robustness

| ID | Status | Idea | Complexity |
|----|--------|------|------------|
| B1 | [ ] | **Circuit breakers for LLM providers** — halt requests to a failing provider after N consecutive errors and recover after a cooldown, using `pybreaker` or similar | Medium |
| B2 | [ ] | **Thread-safe graph store** — wrap NetworkX pickle load/save in a `threading.Lock` (or replace with an in-memory concurrent structure) to prevent race conditions on the server | Medium |
| B3 | [x] | **Structured logging & metrics** — emit JSON log lines and expose a Prometheus endpoint for request latencies, error rates, and LLM call counts | Medium |
| B4 | [x] | **Migrate Anthropic to `/v1/messages`** — drop legacy `/v1/complete` + `claude-2.0`; update template and auth header; use `claude-haiku-4-5` or newer | Low |

### Testing Coverage Gaps

| ID | Status | Idea | Complexity |
|----|--------|------|------------|
| T1 | [ ] | **RAG pipeline integration tests** — spin up a temporary ChromaDB + NetworkX graph and validate retrieval accuracy end-to-end | Medium |
| T2 | [x] | **Failure-scenario fixtures** — pytest fixtures that simulate LLM timeouts, rate-limit 429s, and malformed responses to verify graceful degradation | Low |
| T3 | [ ] | **Frontend component tests** — Jest + React Testing Library coverage for Chat bubble rendering, ERD node interactions, and Ingest Wizard steps | Medium |
| T4 | [ ] | **Load tests** — Locust scenarios for concurrent chat sessions to surface throughput bottlenecks before they hit production | Medium |

### CI/CD Refinement

Current state: one workflow (`ci.yml`) that only runs `pytest` on push to `master`. PR check is explicitly disabled. No linting, no frontend tests, no CD, no branch protection.

| ID | Status | Task | Complexity | Files |
|----|--------|------|------------|-------|
| CI1 | [x] | **Re-enable PR check** — uncomment the `pull_request` trigger in `ci.yml` so tests must pass before any PR can be merged into `master`; this is the single highest-leverage CI change and unblocks all others | Low | `.github/workflows/ci.yml` |
| CI2 | [x] | **Add ruff lint step** — `pyproject.toml` already has ruff config; add a `ruff check .` step to the CI job so style/import errors are caught on every PR before tests even run | Low | `.github/workflows/ci.yml` |
| CI3 | [ ] | **Add frontend Vitest step** — add a second CI job that runs `npm ci && npm test` in `frontend/`; cache `node_modules` with `actions/cache` keyed on `package-lock.json` hash | Low | `.github/workflows/ci.yml` |
| CI4 | [ ] | **Split into parallel jobs** — separate the workflow into three independent jobs (`lint`, `backend-tests`, `frontend-tests`) that run in parallel; reduces total CI wall time; `lint` is a prerequisite gate for the test jobs | Low | `.github/workflows/ci.yml` |
| CI5 | [ ] | **Add pip-audit security scan** — run `pip-audit -r requirements-ci.txt` as a non-blocking CI step to surface known Python CVEs; complement to the existing Dependabot frontend alerts | Low | `.github/workflows/ci.yml`, `requirements-ci.txt` |
| CI6 | [ ] | **Branch protection rules** — on GitHub: require CI to pass + at least one review before merge to `master`; block force-push to `master`; these are repo settings, not workflow changes | Low | GitHub repo settings |
| CI7 | [ ] | **requirements-ci.txt sync check** — add a CI step that diffs `requirements-ci.txt` against `pyproject.toml` optional deps to catch cases where a new runtime dep is added but the CI install file isn't updated | Medium | `.github/workflows/ci.yml` |
| CI8 | [ ] | **CD pipeline (optional)** — add a `deploy` job triggered only on merge to `master` that builds the frontend (`npm run build`) and packages/deploys the backend; target depends on hosting choice (Render, Railway, Docker Hub) | High | `.github/workflows/cd.yml` |

**Recommended order:** CI1 → CI2 → CI3 → CI4 → CI5 → CI6 (CI8 only once a hosting target is decided)

---

### Business Semantic Layer

A glossary-driven enrichment layer that maps business terminology and KPI definitions to their underlying tables, columns, and formulas. When a user asks *"What is our AUM this quarter?"* or *"Show delivery delay by region"*, the system resolves the business term to its canonical definition, math, and table dependencies **before** RAG retrieval runs — so the query generator gets the formula, not just the word.

**Problem this solves:** ChromaDB embeds table descriptions. But business terms like "AUM", "churn rate", or "delivery delay" are not in table descriptions — they live in analysts' heads or wiki pages. Today the LLM guesses the formula; with this layer it retrieves it.

**Architecture:**

```
User query: "Show AUM by fund manager last quarter"
        │
        ▼ SL1 — Term Resolver
  Semantic search against embedded business glossary
  → matches: AUM = "Assets Under Management; SUM(market_value) WHERE account_type='investment'"
  → table_deps: [positions, accounts, fund_managers]
        │
        ▼ Injected into gatherRequirements context:
  ## Business Definitions
  AUM: Assets Under Management
  Formula: SUM(p.market_value) FROM positions p JOIN accounts a ON ...
  Tables: positions, accounts, fund_managers
        │
        ▼ generateQuery now has the formula + the right tables in SCHEMA
  → Generates correct SQL first time, no hallucinated formula
```

**Storage design** — new SQLite table `business_terms` (in `tableMetadata.db`):

```sql
CREATE TABLE business_terms (
    term_id       TEXT PRIMARY KEY,   -- UUID
    term_name     TEXT NOT NULL,      -- "AUM"
    full_name     TEXT,               -- "Assets Under Management"
    definition    TEXT,               -- plain-English description
    formula       TEXT,               -- SQL expression or pseudocode
    formula_type  TEXT,               -- "sql_expression" | "pseudocode" | "description"
    table_deps    TEXT,               -- JSON array of table names
    column_deps   TEXT,               -- JSON array of "table.column" strings
    synonyms      TEXT,               -- JSON array: ["assets under mgmt", "total aum"]
    example_value TEXT,               -- "$4.2B as of Q4 2024"
    domain        TEXT,               -- "finance" | "logistics" | "marketing" | ...
    instance_name TEXT DEFAULT '',    -- scoped to DB instance
    created_at    TEXT,
    updated_at    TEXT
);
```

**ChromaDB collection** — `business_glossary` — embed `term_name + full_name + definition + synonyms` so user queries match terms even when phrased differently (e.g. "managed assets" → "AUM").

**Retrieval integration** — `_get_business_context(query, instance_name)` returns matched terms + formulas. Called in both `gatherRequirements` (injected into `SCHEMA` block alongside table schemas) and `generateQuery` (prepended as `## Business Definitions` section).

| ID | Status | Task | Complexity | Files |
|----|--------|------|------------|-------|
| SL0 | [x] | **Business glossary storage** — Add `business_terms` SQLite table to `tableMetadata.db`; migration helper in `Utilities/base_utils.py`; CRUD wrapper `MetadataManager/GlossaryStore.py` with `add_term`, `get_term`, `delete_term`, `list_terms`, `search_by_name` | Low | `MetadataManager/GlossaryStore.py`, `Utilities/base_utils.py` |
| SL1 | [x] | **Glossary embedding + semantic search** — Embed each term's `term_name + full_name + definition + synonyms` into a new ChromaDB collection `business_glossary`; `_get_business_context(query, instance_name)` does cosine similarity search and returns top-k matched terms with their formulas and `table_deps`; threshold-gated to avoid injecting noise | Medium | `MetadataManager/GlossaryStore.py`, `MetadataManager/MetadataStore/vdb/Chroma.py` |
| SL2 | [x] | **Retrieval integration** — Call `_get_business_context()` in `gatherRequirements` (alongside ChromaDB table search) and in `generateQuery` / `generateQueryStream` (prepend `## Business Definitions` block to schema section); `PromptBuilder.format_schema()` renders glossary_hits list; formula string injected verbatim | Medium | `main.py`, `APIManager/PromptBuilder.py` |
| SL3 | [x] | **Import API** — `POST /api/glossary/terms/single`, `POST /api/glossary/terms/bulk`, `GET /api/glossary/terms?instance_name=&domain=`, `GET /api/glossary/terms/{term_id}`, `PUT /api/glossary/terms/{term_id}`, `DELETE /api/glossary/terms/{term_id}`, `GET /api/glossary/search?q=` | Low | `backend/glossary.py`, `backend/app.py` |
| SL4 | [ ] | **Glossary UI tab** — New "Glossary" tab: searchable table of all terms with inline edit; "Add Term" modal with fields for name, full name, definition, formula, domain, table dependencies (multi-select from known schema), synonyms; bulk CSV/JSON import | Medium | `frontend/src/components/BusinessGlossary.jsx`, `frontend/src/App.jsx` |
| SL5 | [ ] | **Term–table linking in ERD** — Terms with `table_deps` appear as annotation nodes on the Schema/ERD tab; clicking a term node shows its definition and formula; tables linked to the term are highlighted | Medium | `frontend/src/components/SchemaERD.jsx`, `frontend/src/components/BusinessGlossary.jsx` |
| SL6 | [x] | **Tests** — 35 CRUD tests in `test_glossary_store.py`; 19 semantic/injection tests in `test_glossary_retrieval.py`; all 247 tests pass | Low | `tests/test_glossary_store.py`, `tests/test_glossary_retrieval.py` |

**Term JSON schema** (for `POST /api/glossary/terms` and bulk import):
```json
{
  "term_name": "AUM",
  "full_name": "Assets Under Management",
  "definition": "Total market value of all investment assets managed on behalf of clients.",
  "formula": "SUM(p.market_value) FROM positions p JOIN accounts a ON p.account_id = a.account_id WHERE a.account_type = 'investment'",
  "formula_type": "sql_expression",
  "table_deps": ["positions", "accounts"],
  "column_deps": ["positions.market_value", "accounts.account_type"],
  "synonyms": ["assets under management", "total aum", "managed assets"],
  "example_value": "$4.2B as of Q4 2024",
  "domain": "finance",
  "instance_name": "prod_snowflake"
}
```

**Design decisions:**
- **Separate ChromaDB collection** (`business_glossary`) not mixed with table embeddings — keeps retrieval scores comparable within each collection; allows independent similarity thresholds
- **`table_deps` as a pre-hint, not a replacement** — glossary resolves which tables *should* be involved; RAG retrieval still runs and may add more; both merge before entering the prompt
- **Formula injected verbatim** into `## Business Definitions` — LLM instructed to treat it as canonical rather than deriving its own
- **Domain scoping** — `domain` field prevents a finance glossary polluting a logistics instance even within the same `instance_name`
- **Synonym matching** — synonyms concatenated into the ChromaDB document so "managed assets" and "AUM" both resolve to the same entry

**Recommended order:** SL0 → SL1 → SL2 (core value unlocked here) → SL3 → SL6 → SL4 → SL5

---

### Developer Experience

| ID | Status | Idea | Complexity |
|----|--------|------|------------|
| D1 | [ ] | **Docker Compose dev environment** — single `docker compose up` that starts the backend, frontend, and dependency stubs (mock LLM, ChromaDB, SQLite) | Medium |
| D2 | [ ] | **Pre-commit linting hooks** — add `black`, `isort`, `flake8`, and `mypy` as pre-commit hooks for consistent style and early type errors | Low |
| D3 | [ ] | **OpenAPI / Swagger playground** — enable FastAPI's auto-generated interactive docs at `/docs` for backend exploration without a running frontend | Low |
| D4 | [ ] | **Contributor onboarding guide** — architecture diagram + step-by-step local setup in `CONTRIBUTING.md` | Low |
| D5 | [ ] | **FastMCP wrapper** — publish this tool as a FastMCP server that exposes `generateQuery`, `generatePipelineDict`, and other helpers via typed tools/resources so Claude and other MCP clients can invoke them with schema validation | Medium |

### Prioritization Context — Validation & Outcome Layer
*Decided: 2026-03-15 · Branch: `Claude/feature/validation-outcome-layer` · Resolves: GitHub issue #16*

**Why this order:**
The Validation Layer (VL0–VL9) is architecturally sound but its value scales with corpus size — it needs real outcome history to mine. Building it before that history exists produces a sophisticated harness with nothing to test against. The sequence below ensures each phase is justified by real signal before the next is built.

**Deferred and why:**

| Item | Deferred because |
|------|-----------------|
| VL0–VL9 (full Validation Layer) | Needs QT1 running for several weeks first so the corpus has real labeled outcomes — not just session text |
| QT4 (LLM failure classifier) | Start rule-based (regex on SQL error strings covers ~80% of cases); upgrade to LLM classification only once misclassified cases justify the added cost and latency |
| A5 (auth/multi-tenancy) | Only relevant at multi-user deployment; not the current bottleneck |
| A1 (managed DB) | SQLite fine at current scale; Kuzu (A2 done) resolved the main graph store concern |

**Decided build sequence:**

```
Phase 1 — Close real gaps (this branch)
  QT1  Outcome recorder          additive to /api/execute, starts corpus silently
  QT2  UI outcome badge          highest user-visible value, lowest complexity
  B3   Structured logging        observability before anything else is measurable
  T2   Failure-scenario fixtures test the paths that will actually break in prod

Phase 2 — Once QT1 has 2–4 weeks of real data
  QT4  Rule-based failure classifier  (regex taxonomy, no LLM hop yet)
  P2   RAG cache                      measurable latency win
  VL0  Corpus builder                 now has labeled data to work with

Phase 3 — Validation layer proper
  VL1  Contender generator
  VL2  Perturbation engine
  VL3  Canary runner
  VL6  Cron scheduling
  VL4 + VL7  Reports + dashboard
```

**GitHub issue #16** (`[Feature] Add Evaluation Harness for NL→SQL Query Generation`) covers the evaluation goals. This branch delivers Phase 1 and lays the corpus infrastructure that replaces the static `benchmark_cases.json` proposed in that issue with a live, usage-driven corpus.

---

### Query Outcome Tracking

Runtime signal capture: every generated query gets a **success/failure verdict** recorded at execution time. This is the prerequisite feed for the Validation Layer — the corpus (VL0) only has real ground-truth labels if the system knows which of its own outputs actually worked.

```
User sends message
      │
      ▼  /api/chat  →  SQL/code generated
      │
      ▼  /api/execute  →  result rows  ──── success (row_count ≥ 0)
                      →  SQL error    ──── failure (error_type, error_msg)
                      │
                      ▼  QT1 — Outcome recorder
                         Appends {session_id, query_id, nl_query, generated_sql,
                         provider, query_type, outcome, error_type, row_count,
                         latency_ms, ts} to outcome store
                      │
                      ├──▶  QT2 — UI feedback badge
                      │         Green ✓ / Red ✗ on the chat bubble;
                      │         optional "why did this fail?" expansion
                      │
                      └──▶  QT3 — Corpus label feed
                                 VL0 corpus_builder reads outcome store
                                 to attach `outcome` labels to corpus entries
```

| ID | Status | Task | Complexity | Files |
|----|--------|------|------------|-------|
| QT1 | [x] | **Outcome recorder** — after `/api/execute` returns, append a structured outcome record `{session_id, query_id, nl_query, generated_sql, provider, query_type, outcome: "success"\|"failure"\|"empty", error_type, error_msg, row_count, latency_ms, ts}` to `validation/corpus/outcomes.jsonl`; also write a lightweight SQLite table `query_outcomes` for indexed lookups by date/provider/outcome | Medium | `backend/app.py`, `backend/executor.py`, `validation/outcome_store.py` |
| QT2 | [x] | **UI outcome badge** — attach a success/failure indicator to each SQL/code chat bubble: green check + row count on success, red badge + short error type on failure; clicking the badge expands the full error message and a "Report issue" button that flags the record in the outcome store for priority review | Low | `frontend/src/components/ChatMessage.jsx`, `backend/app.py` |
| QT3 | [ ] | **Corpus label feed** — VL0 corpus builder joins against `outcomes.jsonl` when building corpus entries so each record carries `outcome`, `error_type`, and `row_count` from the real execution; this lets the perturbation engine (VL2) weight mutations toward query patterns that already have a failure history | Low | `validation/corpus_builder.py` |
| QT4 | [ ] | **Failure classification** — when an execution fails, a lightweight LLM call (cheap model, single-shot) classifies the error into a taxonomy: `schema_mismatch`, `ambiguous_join`, `missing_table`, `syntax_error`, `permission_denied`, `empty_result_unexpected`; stored as `error_type` in the outcome record and surfaced in daily reports by category | Medium | `validation/failure_classifier.py` |

**Design notes:**
- `outcome: "empty"` is a distinct state from `"success"` — query ran without error but returned zero rows; often indicates a logic problem (wrong filter, wrong table) even though SQL was valid
- `/api/execute` already returns error details — QT1 is purely additive, no changes to existing execution logic needed
- `query_id` is a UUID generated at `/api/chat` time and threaded through to `/api/execute` via the session; no new API contract change needed if session_id is used as the key
- Outcome store (`outcomes.jsonl`) is the single source of truth; the SQLite `query_outcomes` table is a read-optimised index built from it, not a primary store
- QT4 failure classification should be async (fire-and-forget after response is returned) to avoid adding latency to the user-facing path

---

### Validation Layer

Cron-based daily validation system anchored to **real usage history**. Instead of blindly cooking synthetic queries, the antagonist LLM mines the actual questions users have asked, derives contender variants from those, and systematically perturbs them to build a **baseline canary suite**. This ensures every test case is grounded in real intent and schema, not hallucinated coverage.

```
Real user query history
        │
        ▼ VL0 — Query Corpus Builder
  Deduplicated corpus of past NL queries + their generated SQL
        │
        ├──▶ VL1 — Contender Generator (antagonist LLM)
        │         Reads corpus; produces semantically related but
        │         distinct NL questions (different filters, aggregations,
        │         time windows, edge-case thresholds) — still grounded
        │         in real tables the user actually touched
        │
        └──▶ VL2 — Perturbation Engine
                  Applies systematic mutations to existing NL queries:
                  column swap, filter inversion, join-path extension,
                  aggregation change, null-handling challenge, etc.
                  Each mutation is tagged with its perturbation type.
                       │
                       ▼ (corpus + contenders + perturbations)
                  VL3 — Canary Runner
                  Pushes all queries through /api/chat; captures
                  generated SQL + metadata per provider under test
                       │
                       ├──▶ VL4 — Runnability Checker
                       │         Dry-runs each output via /api/execute;
                       │         records pass/fail, error, row count
                       │
                       └──▶ VL5 — Multi-table Join Advisor
                                 When output misses expected tables,
                                 walks NetworkX for join paths and
                                 suggests multi-query tether decompositions
                                       │
                                       ▼
                                 VL6 — Daily Performance Log
                                 Pass rate by perturbation type, provider,
                                 query type; regression delta vs. prior day
                                       │
                                       ▼
                                 VL7 — Prompt Enhancement Feed
                                 Failure clusters → candidate prompt diffs
                                 staged for human review
```

| ID | Status | Task | Complexity | Files |
|----|--------|------|------------|-------|
| VL0 | [ ] | **Query corpus builder** — scrape all past conversation sessions (from `poly_ql_sessions` localStorage export or a new `POST /api/validation/ingest-history` endpoint) and persist a deduplicated corpus of `{nl_query, generated_sql, provider, query_type, tables_touched, outcome, error_type, row_count}` records to `validation/corpus/corpus.jsonl`; joins against `outcomes.jsonl` (QT1) to attach real execution verdicts; re-run nightly to append new sessions | Medium | `validation/corpus_builder.py`, `backend/app.py`, `validation/corpus/corpus.jsonl` |
| VL1 | [ ] | **Contender generator** — antagonist LLM reads the corpus and, for each unique NL query cluster, generates N semantically related but distinct contender questions (different filters, aggregation levels, time windows, edge-case thresholds); contenders stay anchored to the same tables/columns the original query used so they test real schema coverage, not hallucinated tables | High | `validation/contender_gen.py`, `validation/antagonist.py` |
| VL2 | [ ] | **Perturbation engine** — applies a menu of typed mutations to each corpus query: `column_swap` (replace one column with another of same type), `filter_inversion` (negate or flip a WHERE condition), `join_extension` (add one more hop in the NetworkX graph), `aggregation_change` (SUM→AVG→COUNT), `null_challenge` (add `IS NULL`/`IS NOT NULL`), `threshold_shift` (nudge numeric literals), `negation` (ask for records that do NOT match). Each mutated query carries a `perturbation_type` tag for regression grouping. | High | `validation/perturbation.py` |
| VL3 | [ ] | **Canary runner** — combines corpus originals + contenders + perturbations into a single ranked run list; pushes each through `/api/chat` against the provider(s) under test; stores `{input_nl, expected_tables, output_sql, provider, query_type, perturbation_type, latency_ms}` per result in `validation/runs/YYYY-MM-DD.jsonl` | Medium | `validation/runner.py` |
| VL4 | [ ] | **Runnability checker** — dry-runs each output via `/api/execute`; records `{pass, error_msg, row_count}`; flags regressions where a query that previously passed now fails (compares against prior day's run file) | Medium | `validation/checker.py`, `backend/executor.py` |
| VL5 | [ ] | **Multi-table join advisor** — when a canary result's `tables_touched` misses expected tables (known from the corpus original), walks the Kuzu/NetworkX graph for join paths and appends alternative multi-query decompositions + tether suggestions to the failure record | High | `validation/join_advisor.py`, `MetadataManager/MetadataStore/relationdb/networkxDB.py` |
| VL6 | [ ] | **Daily performance log** — aggregates run + checker results into a structured report: overall pass rate, pass rate broken down by `perturbation_type` and `provider`, regression delta vs. prior day, schema coverage gaps, and LLM-generated bullet-point improvement suggestions keyed to each failure cluster; written to `validation/reports/YYYY-MM-DD.json` | Medium | `validation/reporter.py`, `validation/reports/` |
| VL7 | [ ] | **Prompt enhancement feed** — clusters failures by perturbation type and prompt section; formats each cluster as a candidate diff (patch suggestion for `taskGenerateSQL.txt`, `taskRequirementGather.txt`, etc.) and writes to `validation/prompt_suggestions/YYYY-MM-DD.md` for human review before any prompt is committed | High | `validation/prompt_advisor.py`, `APIManager/Prompts/` |
| VL8 | [ ] | **Cron scheduling & config** — `validation_config.YAML`: `cron_expr`, `antagonist_provider`, `providers_under_test[]`, `max_contenders_per_query`, `perturbation_types[]`, `target_instance`, `dry_run`; wired via `APScheduler` or OS cron; `validation/__main__.py` is the entry point | Low | `validation/validation_config.YAML`, `validation/__main__.py` |
| VL9 | [ ] | **Validation dashboard endpoints** — `GET /api/validation/reports` (list summaries), `GET /api/validation/reports/{date}` (full report), `GET /api/validation/corpus/stats` (corpus size, last updated, perturbation type breakdown) | Low | `backend/app.py` |

**Design notes:**
- **QT feeds VL** — `outcomes.jsonl` (QT1) is a hard prerequisite for VL0; the corpus has no ground-truth labels without it; implement QT1 before starting VL0
- **No blind query cooking** — every test question traces back to a real user query in the corpus; contenders are derived variants, perturbations are typed mutations; the antagonist's role is to *challenge and vary*, not to *invent from nothing*
- **Baseline canary = corpus originals** — the unmodified corpus queries are the stable canary; if they regress, something fundamental broke; contenders and perturbations test the *boundaries* of that known-good baseline
- **Perturbation tagging** — every mutation carries its type so regression reports can say "SUM→COUNT perturbations have 40% fail rate" rather than just an aggregate number
- **Corpus as ground truth** — `tables_touched` from the original run is the expected-table set for join advisor and coverage checks; no manual oracle needed
- Antagonist model should be a *different* provider than the one under test to avoid self-evaluation bias
- Runnability re-uses the existing `/api/execute` path — no new DB connections needed
- Join advisor re-uses `_get_full_table_schema()` + Kuzu/NetworkX traversal already in `main.py`
- Daily reports are append-only flat files; no new DB table needed initially
- Prompt suggestions are staging-only — a human reviews `validation/prompt_suggestions/` before any commit

---

---

### C4 — `database.schema.table` Fully-Qualified Name Support

*Identified: 2026-03-16 · Branch: `Claude/feature/validation-outcome-layer`*

Current system stores only bare table names (e.g. `orders`). Users working with multi-database or multi-schema environments use `database.schema.table` notation (e.g. `prod.sales.orders`). This breaks in five distinct places.

**Breaking points:**

| File | Location | How it breaks |
|------|----------|---------------|
| `Utilities/base_utils.py` | `_validate_identifier()` — regex `^[A-Za-z_][A-Za-z0-9_]*$` | Rejects dots — SQLite CRUD fails before executing |
| `backend/ingestion.py` | Source table regex `\w+` (parse_pipeline) | Only captures the leaf table name; silently drops `db.schema` prefix |
| `MetadataManager/MetadataStore/relationdb/kuzuDB.py` | Node key uses `.lower()` of plain name | Graph node names break with dot characters |
| `main.py` | `_get_full_table_schema()` — SQLite lookup by bare `tableName` | Returns nothing for qualified names; LLM gets empty schema |
| ChromaDB metadata | `TableName` stored as-is | Instance filtering (`instance_name`) stops working when TableName includes dots |

**Proposed fix design:**

1. **SQLite schema change**: Add `database_name TEXT DEFAULT ''` and `schema_name TEXT DEFAULT ''` columns to both `tableDesc` and `tableColMetadata` — mirrors the existing `instance_name` pattern. Migration: `ALTER TABLE ... ADD COLUMN` (safe, non-breaking for existing rows).

2. **Ingestion / import**: When storing a table named `db.schema.table`, parse and store components separately. Bare names leave `database_name` and `schema_name` empty (backward-compatible).

3. **Kuzu node key**: Use `{database}__{schema}__{table}` with double underscores as the graph node identifier. Avoids dot issues; double-underscore is unlikely to appear in real names. Plain names become `______{table}` (two empty parts) — or use a helper `_node_key(db, schema, table)`.

4. **Display / prompt reassembly**: Wherever a table name is displayed to the user or injected into a prompt, reassemble as `database.schema.table` (omitting empty parts) for correct SQL output.

5. **`_validate_identifier()`**: Update regex to allow dots (for qualified names) or validate each component separately before a lookup.

**Backward compatibility**: All existing bare-name metadata remains valid — `database_name = ''` and `schema_name = ''` behave identically to the current system.

---

## Known Behaviours / Notes

- **Two-agent loop**: `gatherRequirements()` runs up to 5 `get_schema` tool calls before forcing a final answer. The code fence regex strips any language tag (```json, ```sql, etc.) before JSON parsing.
- **NetworkX graph type**: Existing `Relations.pickle` may be an undirected `nx.Graph`. The lineage endpoint uses `graph.is_directed()` to pick the right neighbor accessor.
- **LLM response format**: All query prompts return JSON `{type, content}`. `validate_sql` falls back gracefully if the LLM returns raw SQL.
- **SQLAlchemy driver**: `sqlalchemy>=2.0` is in `requirements.txt` but DB-specific drivers (e.g. `psycopg2`, `pymysql`) must be installed separately.
- **Row cap**: `/api/execute` returns at most 500 rows via `fetchmany(500)`.
- **Embedding model**: `mixedbread-ai/mxbai-embed-large-v1` loaded from local path in `retrieval_config.YAML`.
- **Claude Code CLI**: Requires `claude` on PATH (`npm install -g @anthropic-ai/claude-code`). Subprocess uses `encoding="utf-8"` to avoid Windows cp1252 decode errors.
- **Conversation history**: Stored in `localStorage` under key `poly_ql_sessions`. Sessions include full message list, provider, and query type so they can be fully resumed.
- **Anthropic Messages API**: `anthropic` provider uses `/v1/messages` with `claude-3-5-haiku-20241022`. `claude_code` uses the CLI (bypasses API entirely).

## Codex Work Log

### Requirements & Plan (Codex)
- Requirement (2026-03-08): Document all Codex work in this file. Review the Claude Code guidance, ensure Claude balance + clarify UX are covered, and reflect every step in tests and UI behaviour. Plan: inspect balance/usage modules, add `_check_claude_code` regression tests, extend ChatInterface clarify/provider tests (with a scroll-to-bottom stub), and rerun backend/frontend suites, logging results.

### Actions Completed
- 2026-03-08: Reviewed `backend/balance.py`, `backend/usage_tracker.py`, `APIManager/AllAPICaller.py`. Added `tests/test_balance.py` to cover CLI missing/available/usage labels (pytest run succeeded). Recorded the initial Vitest `esbuild` `EPERM` failure, then stubbed `scrollIntoView`, added `frontend/src/components/__tests__/ChatInterface.test.jsx` to confirm Claude dropdown labels, availability disabling, and clarify-option pills, and reran `npm test -- ChatInterface` successfully.

### Requirements & Plan (Codex)
- Requirement (2026-03-08): The lineage view is showing generic neighbor data instead of the actual lineage subgraph, and the ingest pipeline dropdown lacks the same provider formatting and balance-awareness as the query tab. Plan: expand `/api/lineage/{table}` to return the connected lineage subgraph (nodes + edges) so the UI can render true lineage, centralize provider labels/formatting, update ChatInterface + IngestTable selects to reuse them, fetch provider balances for ingest, and add component tests to lock in the new dropdown behavior.

### Actions Completed
- 2026-03-08: Updated `backend/app.py` so `/api/lineage/{table}` now returns every node/edge in the connected component via `networkx.node_connected_component`, removed the old neighbor-only logic, and kept the `joinKeys`. Added `frontend/src/constants/providerLabels.js`, wired `ChatInterface.jsx` and `IngestTable.jsx` to reuse the provider label helper, fetch provider balances for the ingest dropdown, and newly added `frontend/src/components/__tests__/IngestTable.test.jsx` to assert balance-aware rendering. Ran the backend and frontend suites (pytest + `npm test -- ChatInterface` + `npm test -- IngestTable`).

### Requirements & Plan (Codex)
- Requirement (2026-03-08): Integrate the OpenAI Codex provider into the Query Generation UI so users can select it alongside the other models, and ensure both query and ingest dropdowns stay in sync with the backend's finance data.
- Plan: make the App-level provider list include Codex, expand the App and IngestTable component tests to cover the Codex label/availability plumbing, and rerun the relevant Vitest suites before logging the results.

### Actions Completed
- 2026-03-08: Extended `App.test.jsx` so the mocked `/api/providers` response defaults to `['anthropic', 'codex', 'open_ai']` and verified the dropdown options now include Codex. Added a Codex-specific assertion to `frontend/src/components/__tests__/IngestTable.test.jsx` so the ingest dropdown renders the `OpenAI Codex` label from `providerLabels`. Ran `npm test -- App`, `npm test -- ChatInterface`, and `npm test -- IngestTable` to confirm coverage.
- 2026-03-08: Tightened the Codex test so it no longer relies on the encoded dash sequence; the assertion now matches any option whose accessible name contains `OpenAI Codex` and also checks the `$0.03` label, keeping the dropdown coverage resilient to minor spacing/encoding differences.

### Requirements & Plan (Codex)
- Requirement (2026-03-08): Package the query/ingest/lineage helpers as FastMCP tools so Claude (and other MCP clients) can call them with schema validation and streaming results.
- Plan: create FastMCP tool definitions for `generateQuery`, `generatePipelineDict`, and relevant fetch helpers, document their JSON schemas and prompt/resource expectations, and add a README entry that explains how to run the FastMCP server locally before publishing.

### Actions Completed — 2026-03-11 (B4)
- Confirmed `ANTHROPIC.json` already uses `/v1/messages` + `claude-3-5-haiku-20241022` (migration was complete but untracked). Updated `TASK.md` provider table and Known Behaviours note. Added `TestCheckAnthropic` (4 tests: invalid key, no credits, valid key/N/A, network error) to `tests/test_balance.py` — all pass.

### Actions Completed — 2026-03-11
- feat: multi-DB instance support across all storage layers (SQLite, ChromaDB, NetworkX)
  - `instance_name` + `db_type` columns added to `tableDesc` and `tableColMetadata` (with ALTER TABLE migration)
  - NetworkX pickle migrated to `{instance_name: DiGraph}` dict format (backward-compatible)
  - ChromaDB queries filter by `DB=instance_name` when not "default"
  - New `GET /api/instances` endpoint; all schema/chat/ingest endpoints accept `instance_name`
  - Frontend: instance selector in ChatInterface, SchemaERD; DB type + instance name in IngestTable
  - `DB_TYPES` constant added to `providerLabels.js`
- feat: replaced Data Lineage tab with Join Path Explorer
  - New `GET /api/joinpath?from_table=X&to_table=Y` returns shortest join path with cardinality
  - New `GET /api/derivatives/{table}` returns pipeline parent/child tables
  - `DataLineage.jsx` rewritten: Join Path tab + Derivative Tables tab
  - Tab renamed "Data Lineage" → "Join Path" in `App.jsx`
### Requirements & Plan (Codex)
- Requirement (2026-03-11): Review Claude's workspace changes, focus on the latest DataLineage/Join Path tweaks, rerun the frontend suites, and log the results so the change log matches reality.
- Plan: inspect the current buildFlow/edge rendering changes, run `npm test -- App ChatInterface IngestTable`, and capture both successes and the environment-level EPERM failures in this log.

### Actions Completed - 2026-03-11
- Confirmed `buildFlow` now accepts the optional `joinTypeFilter`, reuses the shared `joinTypeColors` map, and still shows the join key labels per edge.
- Tried `npm test -- App ChatInterface IngestTable` plus the standalone `ChatInterface`/`IngestTable` suites, but each run stops while `esbuild` tries to bundle `vite.config.js` because the spawned service is blocked with EPERM. Overriding `ESBUILD_BINARY_PATH` and redirecting `TMP`/`TEMP` to `./tmp` did not remove the restriction, so Vitest remains unable to start until the environment lets `esbuild` spawn again.
- Added `_preload_schemas_bulk` coverage (`tests/test_main_preload.py`), adjusted `tests/test_api.py` and `tests/test_filters.py` for the new `instance_name` plumbing, reran `pytest tests/` with `PYTHONPATH='.'` (all 101 tests pass), and committed the staged changes (main.py, pyproject.toml, TASK.md, DataLineage.jsx, plus the new tests) with both agents as co-authors.

### Actions Completed — 2026-03-11 (A3)
- `retrieval_config.YAML`: added `gather_requirements.max_tool_calls: 5` config key.
- `main.py` (`gatherRequirements`): replaced hard-coded `_MAX_TOOL_CALLS = 5` with a runtime read of `get_config_val("retrieval_config", ["gather_requirements", "max_tool_calls"])` (with `except` fallback to 5); added required `from Utilities.base_utils import get_config_val` local import at top of function.
- `tests/test_gather_config.py`: 3 new tests — config read succeeds, KeyError fallback, invalid-value fallback. All 108 tests pass.
- `AGENTS.md`: updated with Active Agents table, collaboration rules, and explicit handoff protocol.
- → Codex: please commit staged files (`main.py`, `Utilities/retrieval_config.YAML`, `tests/test_gather_config.py`, `AGENTS.md`, `TASK.md`) with message `feat(config): make gather_requirements tool-call cap configurable via retrieval_config.YAML (A3)`. Mark A3 `[x]` in backlog (already done above).

### Actions Completed — 2026-03-11 (A2)
- Added `tests/test_kuzu.py` to cover the Kuzu-backed graph store API (`getObj`, `addRelations`, `getRelations`, and pickle migration) and verified the helpers point at a temporary database directory.
- Ran `PYTHONPATH='.' pytest tests/` after the new tests to confirm the full suite (105 tests) passes on the current branch.

### Requirements & Plan (Codex)
- Requirement (2026-03-11): Capture the Kuzu-based lineage work, rerun the live suites, and log the barriers so the follow-up commit knows what still needs attention.
- Plan: describe the `buildFlow` join-type filters and provider label updates, attempt `npm test -- App ChatInterface IngestTable`, and run the targeted Python tests under `PYTHONPATH='.'` for the new Kuzu helpers.

### Actions Completed - 2026-03-11
- Confirmed `buildFlow` now respects the shared `joinTypeColors` map + optional `joinTypeFilter`, which keeps the join-key labels while allowing semantic filters.
- Tried `npm test -- App ChatInterface IngestTable` plus the individual `ChatInterface`/`IngestTable` suites; each run stops while `esbuild` bundles `vite.config.js` because the spawned service is blocked with `EPERM`, so Vitest cant start until that OS-level restriction is removed.
- Ran `PYTHONPATH='.' pytest tests/test_kuzu.py tests/test_lineage.py`; all 6 tests pass under the current environment (warning about Starlette formparsers is unrelated).

### R1 Planning (Codex)
- Requirement (2026-03-13): Plan adaptive re-retrieval agent. See R1_PLAN.md for full design.

### Actions Completed — R1 + P1 (Codex, 2026-03-13)
- Ran full pytest suite: 117 tests pass (9 new in `tests/test_adaptive_retrieval.py`).
- Fixed test isolation bug in `tests/test_kuzu.py::test_getObj_attempts_pickle_migration` — patched `_kuzu_base_dir` directly and cleared `_DB_POOL`/`_SCHEMA_READY` module state.
- Confirmed P1 streaming: `CallServiceStream` in `AllAPICaller.py`, `generateQueryStream` in `main.py`, `/api/chat/stream` SSE endpoint, `_callApiStream` + streaming bubble in frontend.
- Confirmed R1 adaptive retrieval: `_adaptive_retrieval()` + `_is_retrieval_confident()` in `main.py`, config in `retrieval_config.YAML`, 9 tests all pass.
- Both P1 and R1 marked `[x]` in backlog. Commits made.

---

## R1 — Adaptive Re-retrieval: Design Notes

*Implemented: 2026-03-13. Design authored by OpenAI Codex (o4-mini). See commit history for implementation.*

### Architectural placement decision

`_adaptive_retrieval()` lives in **`main.py`** as a private helper — not in `SQLBuilderComponents.py` and not in a separate file.

| Option | Decision |
|--------|----------|
| `main.py` helper | ✅ Chosen — same module as call site; direct access to `_get_table_directory()` and `CallLLMApi`; keeps SQLBuilderComponents infrastructure-only |
| Inline in `generateQuery` | ❌ Already complex function; harder to test in isolation |
| New `retrieval_agent.py` | ❌ Over-engineering for one function |
| Inside `SQLBuilderComponents.py` | ❌ Mixes concerns — that file is intentionally infrastructure-only (ChromaDB + NetworkX + SQLite) |

### Confidence check

Confidence = `len(direct_tables) >= min_direct_tables`. Table count is the right signal because:
- Reranker filtering already ran inside `SQLBuilderSupport.__filterRelevantResults__` before `_adaptive_retrieval` sees the context
- Surviving direct table count is what `generateQuery` actually needs; raw ChromaDB cosine distance is a coarser pre-filter
- Default `min_direct_tables: 2` catches the common fact-table + dimension pattern while allowing single-table queries to pass on first round

### R1 must NOT run inside `gatherRequirements`

`gatherRequirements` already has a native schema-discovery mechanism: the `get_schema` tool loop (up to `max_tool_calls`). Adding R1 there would:
1. Double LLM call count during the already-costly gather phase
2. Be redundant — the gather LLM can directly call `get_schema("X")` for any table it needs
3. Increase clarification latency significantly

R1 addresses a specific failure mode: **`generateQuery` receiving an empty schema** — which is a `generateQuery` boundary problem, not a `gatherRequirements` problem.

### Edge cases handled

| Case | Handling |
|------|----------|
| Empty schema (`_get_table_directory` returns `"(no tables…)"`) | Skip loop entirely; single `getRelevantContext` call |
| Stagnation (same tables found on consecutive rounds) | Stop early — compare `set(direct_tables)` round N vs N-1 |
| Empty rewrite returned by LLM | `break` immediately |
| Identical rewrite to previous query | `break` immediately |
| LLM call fails on rewrite | Catch exception, return best context seen so far |
| Max rounds exhausted | Return accumulated best-context; `generateQuery` handles `no_tables` downstream |

### Config (`Utilities/retrieval_config.YAML`)

```yaml
re_retrieval:
  max_rounds: 3          # total attempts (initial + up to 2 rewrites)
  min_direct_tables: 2   # stop early when this many direct tables found
  rewrite_provider: null # null = same provider as generateQuery caller
```

---

## Development Session History

*Pre-history log from `joblog.md` (sessions 1–6, 2026-02-18 to 2026-02-19). These predate the Change Log section above.*

### Session 1 — 2026-02-18 (Initial architecture review)
- Full codebase exploration and architecture review
- Identified security issues: `eval()` usage (S1, S2), secrets in config (S3)
- Identified architectural weaknesses across retrieval, prompt building, graph layer, and utilities
- Created task list (15 tasks across 5 tiers)
- Notable: `__filterAdditionalColumns__()` was a confirmed no-op placeholder; scoring pipeline in `RAGPipeline.py` ran but output was discarded

### Session 2 — 2026-02-18 (Security fixes)
- **S3**: Verified `model_access_config.YAML` was never committed — keys safe
- **S1**: Replaced `eval()` with `json.loads()` in `AllAPICaller.py`; also fixed trailing commas in `ANTHROPIC.json`, `GROQ.json`, `GOOGLE.json` (were invalid JSON — the original reason `eval` was used)
- **S2**: Replaced `eval()` with `ast.literal_eval()` in `base_utils.py` cache read; narrowed bare `except` to `(ValueError, SyntaxError)`

### Session 3 — 2026-02-18 (Code quality)
- **Q1**: Removed hardcoded absolute paths in `base_utils.py` — added `PROJECT_ROOT` via `pathlib.Path(__file__)`; `config.yaml` updated to use relative filenames
- **Q2**: Fixed double `__set_apidict__()` call — `__init__` was overwriting the side-effect result; `CallService` no longer re-calls it
- **Q3**: Fixed `.gitignore` — `/__pycache__` → `**/__pycache__` for nested cache dirs
- **Q4**: Replaced all `print()` with `logging.getLogger(__name__)` across `SQLBuilderComponents.py`, `Chroma.py`, `importData.py`; also fixed bare `except` in `importData.py` with exception chaining

### Session 4 — 2026-02-18 (RAG pipeline)
- **C1**: Structured prompt template — `PromptBuilder.format_schema()` converts context dict to markdown DDL; `taskGenerateSQL.txt` prompt template created; `main.py` no longer uses raw f-string prompt
- **C2**: Wired reranker scores into filtering — `reranker_threshold: 0.0` in `retrieval_config.YAML`; `__filterRelevantResults__` now filters + sorts by score
- **C3**: Implemented `__filterAdditionalColumns__()` — BM25Okapi scores each column against user query; PRIMARY/FOREIGN KEY columns always retained; falls back to all columns if none pass threshold

### Session 5 — 2026-02-19 (Graph + LLM resilience)
- **M1**: Switched `nx.Graph` → `nx.DiGraph`; `addRelations()` adds edges in both directions (bidirectional JOIN semantics; DiGraph requires explicit reverse edges for `shortest_path`)
- **M2**: Removed redundant bare `except` in `importData.py` that was discarding exception chain
- **M3**: Moved module-level `get_config_val` calls in `RAGPipeline.py` into class `__init__`; removed unused ML library imports (`Detoxify`, `AutoTokenizer`, etc.)
- **M4**: Added exponential backoff retry to `CallLLMApi.CallService()` — retries on `{429, 500, 502, 503, 504}`; delay 1s → 2s → 4s; non-retryable codes fail immediately

### Session 6 — 2026-02-19 (Interfaces + test suite)
- **F4**: Fixed `cachefunc.close()` — was referencing `self.connection` (doesn't exist); corrected to `self.DBObj.connection.close()`
- **F2**: Created `Utilities/store_interface.py` with `BaseMetadataStore` ABC; `accessDB` now inherits from it
- **F1**: Created `MetadataManager/MetadataStore/vdb/base.py` with `BaseVectorStore` ABC; `ChromaVectorStore` class implements it; `RAGPipeline.py` uses interface methods throughout
- **F3**: Created initial test suite — `test_prompt_builder.py` (12 tests), `test_filters.py` (10 tests), `test_base_utils.py` (8 tests)
- All 15 original tasks across tiers 1–5 complete at end of this session
