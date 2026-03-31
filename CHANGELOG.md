# Changelog

All notable changes to Poly-QL are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

#### Business Semantic Layer (SL0–SL3, SL6)
- **SL0 — GlossaryStore CRUD** — `MetadataManager/GlossaryStore.py`: `business_terms` SQLite table auto-migrated into `tableMetadata.db`; full CRUD (`add_term`, `get_term`, `get_term_by_name`, `update_term`, `delete_term`, `list_terms`, `search_by_name`); JSON fields (`table_deps`, `column_deps`, `synonyms`) serialised transparently
- **SL1 — Glossary embedding + semantic search** — `index_term()` embeds `term_name + full_name + definition + synonyms` into a dedicated `business_glossary` ChromaDB collection; `get_business_context()` cosine-similarity searches top-k terms, distance-threshold gated, SQLite-enriched, graceful fallback to `[]`
- **SL2 — Retrieval integration** — `_get_business_context()` in `main.py` called before prompt assembly in `generateQuery`, `generateQueryStream`, and `gatherRequirements`; `PromptBuilder.format_schema()` renders matched terms as a `## Business Definitions` block (term name, full name, formula, table dependencies, example value) prepended to the schema section so the LLM uses canonical formulas
- **SL3 — Glossary REST API** — `backend/glossary.py` (`APIRouter`): `POST /api/glossary/terms/single`, `POST /api/glossary/terms/bulk`, `GET /api/glossary/terms`, `GET /api/glossary/terms/{id}`, `PUT /api/glossary/terms/{id}`, `DELETE /api/glossary/terms/{id}`, `GET /api/glossary/search?q=`; registered in `backend/app.py`; CORS updated to allow `PUT`
- **SL6 — Tests** — 35 CRUD unit tests (`tests/test_glossary_store.py`) and 19 semantic/injection tests (`tests/test_glossary_retrieval.py`); all 247 suite tests pass

- **BM4 — Universal ingestion agent** — `benchmark/ingest_agent.py`: LLM-powered sub-agent (`claude-opus-4-6` + tool use) that accepts any benchmark data directory, auto-detects schema format (BIRD/Spider tables.json, SQL CREATE TABLE files, CSV metadata, custom JSON/YAML), and ingests every database into PolyQL's metadata store; tools: `list_directory`, `read_file`, `store_table`, `clear_instance`; full agentic loop with resume-safe ingestion; CLI: `python -m benchmark.ingest_agent --data_dir ... --instance_prefix ... [--db_ids ...] [--force]`
- **Bug fix — isolated table nodes missing from Kuzu graph** — `backend/ingestion.py`: `store_table()` now always calls `_merge_node()` for the table being stored, so tables with no declared foreign keys still appear in the JOIN-path graph and don't raise `NodeNotFound` during query retrieval
- **Bug fix — retrieval backend mismatch** — `SQLBuilderComponents.py`: `Relations(strgType="networkx")` → `strgType="kuzu"` so query-time JOIN path lookup uses the same Kuzu backend that ingestion writes to; previously all multi-table queries returned empty join paths
- **Bug fix — reranker threshold** — `Utilities/retrieval_config.YAML`: `reranker_threshold` corrected from `0.0` to `-5.0`; `FlagReranker.compute_score()` outputs raw logits (typically −10 to +3) — the previous 0.0 cutoff silently discarded all retrieved tables, making the schema section empty for every query
- **BM0–BM3 — Benchmark evaluation harness** — `benchmark/` module supports BIRD and Spider text-to-SQL benchmarks; `ingest_bird.py` / `ingest_spider.py` parse `tables.json` and populate Poly-QL metadata per instance; `run_inference.py` runs batch SQL generation (resume-safe JSONL, evidence injection for BIRD, per-question latency); `evaluate.py` computes EX (Execution Accuracy) and VES (Valid SQL rate) with breakdown by difficulty and db_id; summary CSV export

### Planned (Phase 2 — after QT1 accumulates real data)
- **SL4** Glossary UI tab — searchable table, Add Term modal, bulk CSV/JSON import
- **SL5** Term–table annotation nodes on the Schema/ERD tab
- **QT4** Rule-based failure classifier — taxonomy-driven error categorisation (schema_mismatch, ambiguous_join, syntax_error, …)
- **P2** In-process RAG cache — TTL dict to skip repeated ChromaDB lookups for the same query
- **VL0** Query corpus builder — joins QT1 outcomes into a labeled corpus for the validation layer
- **CI3** Frontend Vitest step in CI
- **CI4** Parallel lint / backend-test / frontend-test jobs

---

## [0.0.1] — 2026-03-21

Initial public release. Full-stack NL→SQL assistant with multi-provider LLM support, schema management, streaming, and observability.

### Added

#### Core Query Engine
- **Two-agent query flow** — `gatherRequirements()` runs a tool loop (up to configurable `max_tool_calls`) to fetch table schemas before generating; `generateQuery()` / `generateQueryStream()` produce the final SQL using the assembled context
- **Adaptive re-retrieval (R1)** — when ChromaDB returns weak results, an LLM rewrites the search query using the table directory and retries up to `max_rounds` times; stagnation and empty-rewrite early-exits prevent wasted calls
- **Batch schema preloading** — `_preload_schemas_bulk()` replaces N × 2 per-table SQLite round-trips with 2 bulk queries at gather startup
- **Clarifying questions with option pills** — gathering agent returns `options[]` alongside its question; rendered as clickable pill buttons in the chat UI; retry strips assistant clarify history for a clean re-evaluation
- **Multiple output modes** — SQL · Spark SQL · PySpark DataFrame API · Pandas

#### LLM Providers
- **OpenAI** — GPT-4o mini · GPT-4o · GPT-4.1 · GPT-3.5 Turbo
- **OpenAI Codex** — o4-mini · o3-mini · o3 · o1
- **Anthropic** — Claude 3.5 Haiku · Claude 3.5 Sonnet · Claude Sonnet 4.6 · Claude Opus 4.6
- **Google Gemini** — 2.0 Flash · 2.0 Flash Lite · 1.5 Pro · 1.5 Flash
- **GROQ** — Gemma 7B · Llama 3.3 70B · Llama 3.1 8B · Mixtral 8x7B
- **Claude Code CLI** — runs `claude -p` as a subprocess; no API key required
- **Provider balance endpoint** — `GET /api/providers/balance` checks credit/availability concurrently; greyed-out UI for invalid/depleted providers
- **Token usage tracker** — `backend/usage_tracker.py` records Claude Code CLI call counts, tokens, and estimated cost per session
- **Exponential backoff retry** — `CallLLMApi.CallService()` retries on HTTP 429/500/502/503/504 (1 s → 2 s → 4 s); non-retryable codes fail immediately
- **Token-by-token SSE streaming** — `POST /api/chat/stream` streams LLM output via Server-Sent Events; frontend renders tokens incrementally in a live bubble; `done` event finalises

#### Schema Management
- **ChromaDB vector store** — table descriptions embedded with `mxbai-embed-large-v1`; BM25 + cross-encoder reranker filtering pipeline
- **Kuzu embedded graph DB** — replaces NetworkX pickle; Cypher-backed JOIN path queries; auto-migrates `Relations.pickle` on first run
- **Multi-database instance support** — `instance_name` + `db_type` columns on all metadata tables; ChromaDB metadata filter; separate Kuzu DB per instance; `GET /api/instances`
- **Qualified table names (C4)** — `database.schema.table` notation supported end-to-end in pipeline ingestion; backtick/double-quote/bracket/bare quoted forms all parsed correctly

#### Pipeline Ingestion
- **Ingest Table** — paste `INSERT INTO … SELECT` or `CREATE TABLE … AS SELECT`; LLM auto-generates full data dictionary (`tableDesc`, column descriptions, derivation logic); 2-step review wizard before commit
- **Pipeline lineage** — source → target table edges stored in Kuzu; `GET /api/derivatives/{table}` returns parent/child tables

#### Frontend
- **Chat tab** — multi-turn conversation with left-pane session history (30 sessions in `localStorage`); session auto-save; streaming bubble with `●●●` placeholder during generation
- **Schema / ERD tab** — interactive React Flow entity-relationship diagram; click any table for data dictionary side pane
- **Ingest Table tab** — 2-step wizard matching the backend preview/commit flow
- **Join Path tab** — select any two tables, get the shortest JOIN route with join keys and cardinality; Derivative Tables sub-tab for lineage
- **Provider / model toolbar** — dropdowns for provider and model per session; balance-aware (invalid/depleted entries greyed out)
- **Outcome badge** — green ✓ / amber ○ / red ✕ inline with Execute button; session pane dot reflects last query outcome

#### Auth & Sessions
- **User accounts** — register / login with username + password; JWT tokens; `GET /api/auth/me`
- **Sign in with Google** — OAuth 2.0 SSO; `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` env vars; button auto-appears when configured

#### Observability
- **JSON structured logging** — `backend/logging_config.py`; `_JsonFormatter` emits single-line JSON per record; called once at FastAPI lifespan startup; every HTTP request logged with method, path, status, and `latency_ms`
- **Prometheus metrics** — `backend/metrics.py`; in-memory counters (no external dependency); tracks request counts by method/path/status, latency histograms, LLM call counts and errors by provider; `GET /metrics` returns valid Prometheus text format

#### Query Outcome Tracking
- **Outcome recorder (QT1)** — every `/api/execute` call appends a structured record `{session_id, nl_query, generated_sql, provider, query_type, outcome, error_type, error_msg, row_count, latency_ms, ts}` to `validation/corpus/outcomes.jsonl` and a SQLite index `outcomes.db`
- **UI outcome badge (QT2)** — success/failure/empty verdict displayed inline with the Execute button; session pane dot updated per outcome

#### Testing & CI
- **193-test pytest suite** — all external dependencies mocked in `tests/conftest.py`; no API keys or ML models required
- **Failure-scenario fixtures (T2)** — 16 tests covering LLM timeout, HTTP 429, malformed/empty LLM response, network `ConnectionError`, and `/api/execute` DB errors
- **CI pipeline** — GitHub Actions on every PR to `master`; ruff lint (scoped to `backend/`, `tests/`, `validation/`) runs before tests

### Architecture
- FastAPI backend (`backend/app.py`) with lifespan startup, HTTP middleware, and SSE streaming
- Vite + React frontend with React Flow for graph visualisations
- SQLite for table/column metadata and auth; ChromaDB for embeddings; Kuzu for graph relations; JSONL + SQLite for outcome history
- `pyproject.toml` with optional `dev` and `graph-db` extras; `requirements-ci.txt` for CI install

---

*See [TASK.md](TASK.md) for the full backlog and [docs/](docs/) for Mermaid architecture diagrams.*
