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
- **SL4 — Glossary tab** — new `frontend/src/components/BusinessGlossary.jsx` tab: searchable term list (name, definition, formula, synonyms) with instance and domain filters; add / edit form with chip inputs for tables (suggested from the schema), columns and synonyms; delete with confirmation; bulk import from CSV (quoted fields, `;`-separated lists) or JSON (array or the `GET /api/glossary/terms` shape) with a preview of skipped rows; JSON export of the filtered list; Re-index. Parser in `frontend/src/utils/glossaryImport.js` with 8 Vitest tests
- **Bug fix — glossary terms added through the API were never searchable** — `POST`/`PUT /api/glossary/terms…` only wrote SQLite, so terms never reached the `business_glossary` embeddings that feed *Business Definitions* in the prompt (only Ossie imports were embedded). Every create, update and bulk import now embeds the term (model loaded once per process); delete removes the embedding (`GlossaryStore.unindex_term()`); new `POST /api/glossary/reindex` re-embeds existing terms. Embedding is best effort: terms are saved when the model is unavailable and the response says `"indexed": false`
- **Bug fix — glossary API was unauthenticated** — `/api/glossary/*` now requires login like every other router
- **Glossary API fixes** — `POST /api/glossary/terms` accepted no body (untyped parameter) and now takes a term or a list; `POST /terms/bulk?upsert=true` updates terms by name (case-insensitive, per instance) so re-importing a file doesn't duplicate; a bulk request with a nameless term writes nothing; `POST /terms/single` and renames via `PUT` return 409 when the name already exists in the instance; `PUT` can rename (`term_name`). 13 new tests (`tests/test_glossary_api.py`)
- **Measurement tooling** — `benchmark/run_inference.py --without okf,glossary,evidence` runs the benchmark with a context source switched off (records carry `without`; resuming into a file from a different ablation is refused); new `benchmark/compare.py` compares evaluated runs on their common questions — EX / VES / median latency, ΔEX, won / lost question ids and an exact McNemar p-value; new `benchmark/mcp_coldstart.py` starts the MCP server over stdio N times and reports handshake, `tools/list`, first (cold) and warm `search_schema` latency with median/min/max, plus a JSON report. 12 new tests (#30)
- **Knowledge tab: OKF bundle management + export** — new `frontend/src/components/KnowledgePanel.jsx` tab: import an OKF bundle by server path (optional bundle name), list imported bundles per instance with concept counts and remove them, download the instance as an OKF bundle; import an Ossie model by file upload or paste and download the instance as Ossie YAML. Backend (`backend/okf.py`): `GET /api/okf/bundles`, `DELETE /api/okf/bundles?instance_name=&bundle=` (omit `bundle` to clear the instance), `GET /api/okf/export?instance_name=` (zip). Listing and removal open the ChromaDB collection without loading the embedding model. `OKFStore.import_bundle()` tags concepts with a `bundle` name (default: directory name) and **re-importing a bundle now replaces its concepts**, so concepts deleted from the bundle no longer linger; bundles imported earlier appear as `(unnamed)` and can be removed. `OKFStore.export_bundle()` writes OKF v0.2: one `Table` concept per table (schema table, join links with cardinality, column expressions), one `Metric` concept per glossary term (formula, links to the tables it uses) and a root `index.md` with `okf_version` — the exported bundle re-imports onto the same tables. `OssieStore.read_instance()` shared by both exports; 13 new tests (#30)
- **Apache Ossie (OSI) import / export** — new `MetadataManager/OssieStore.py` + `backend/semantic.py` (`POST /api/semantic/import/ossie`, `GET /api/semantic/export/ossie`, authenticated). Datasets → tables (physical name from `source`; single-column PK/unique keys → `PRIMARY KEY`/`UNIQUE`, composite parts marked `COMPOSITE KEY PART`; non-trivial field expressions kept as column logic), relationships → fk join edges, metrics → glossary terms (upserted by name, embedded for search). Accepts the current draft and the 0.1.x `semantic_model` wrapper. Export writes `0.2.0.dev0`, orients relationships so `from` is the many side, and puts many-to-many joins in a `custom_extensions` entry. Checked against the official TPC-DS example (5 tables, 4 relationships, 8 metrics); exports validate against `ossie-schema.json`; round trip is lossless. `store_table()` gains `vdb=` (reuse one embedding model for bulk imports) and `replace=` (no duplicate rows on re-import) and passes `logic_type`; `joinpath` handles qualified table names in join keys; docs in `docs/ossie.md`; 34 new tests (#30)
- **Join resolution for normalised schemas** — new `MetadataManager/MetadataStore/relationdb/joinpath.py`: join edges get a cardinality from PRIMARY KEY / UNIQUE columns (`load_unique_columns()`, one query) and n:m hops cost 3× so paths prefer key-to-key joins; all requested tables are connected with **one** approximate Steiner tree (networkx) instead of merged pairwise shortest paths; join alternatives are kept per hop. Edges now have a `Kind` (`fk` | `lineage`; Kuzu `ALTER TABLE JoinRel ADD Kind` runs once on existing DBs); pipeline-ingest auto edges are stored as `lineage`, and lineage or keyless edges are never used for joins (they stay in the graph for lineage views). `kuzuDB.getRelations()` delegates to the planner (relations gain `cardinality`); the prompt's `## Join Paths` tags each hop and warns about row multiplication on 1:n / n:m hops; `/api/joinpath` and MCP `find_join_path` use the planner and return every alternative; the Join Path explorer lists join conditions per hop with alternatives. `_determine_join_type` reuses `joinpath.join_type`; dead helpers removed from `backend/app.py`; 10 new tests (#30)
- **MCP: bare-column checks, MySQL/MariaDB read-only, agent skill** — `validate_sql` now also checks unqualified columns against the FROM/JOIN tables when every source is a known table (aliases, functions and keywords skipped; CTEs/subqueries not judged). `execute_read_only()` runs MySQL/MariaDB in `SET SESSION TRANSACTION READ ONLY`. **Bug fix:** MariaDB has no `max_execution_time`, so every MariaDB `execute_sql` failed — it now uses `max_statement_time`. Verified against real MariaDB 10.11 and PostgreSQL 16: writes and DDL refused by the read-only transaction, 5 s sleeps cut at a 1 s timeout, data untouched. New `docs/agent-skill/polyql-sql/SKILL.md` teaches agents the PolyQL workflow; 7 new tests (#30)
- **MCP server: execution, generate mode, auth, lineage, resources** — `backend/mcp_server.py`: `execute_sql` runs one read-only SELECT against a server-side connection (`POLYQL_MCP_DB[_<INSTANCE>]`, never a tool argument): rejected unless a single `sqlparse` SELECT, always rolled back, PostgreSQL `SET TRANSACTION READ ONLY` + `statement_timeout`, MySQL `max_execution_time`, SQLite `PRAGMA query_only` + progress-handler timeout, row cap (`POLYQL_MCP_MAX_ROWS`), credentials redacted from errors, outcome recorded with `provider="mcp"` (`backend/executor.execute_read_only()`); `generate_sql` (opt-in `--enable-generate`) wraps `generateQuery`; streamable-http bearer auth via `POLYQL_MCP_TOKEN` and/or PolyQL login JWTs (`JWT_SECRET`), refuses non-local hosts without auth unless `--insecure-no-auth`; `get_lineage` / `get_derivatives` tools; resources `polyql://tables/{instance}`, `polyql://schema/{instance}/{table}`, `polyql://glossary/{term}`; `validate_sql` now checks qualified columns against `tableColMetadata` (`unknown_columns`). `SELECT … INTO` rejected as a write; `sqlalchemy` added to `requirements-ci.txt` for a real-SQLite execution test; 31 new tests (#30)
- **MCP server (context mode)** — `backend/mcp_server.py` (MCP Python SDK v2 `MCPServer`) exposes Poly-QL to agent harnesses: `list_instances`, `list_tables`, `search_schema` (same context as Poly-QL's own generation: business definitions, OKF knowledge notes, tables, join paths), `get_table_schema`, `find_join_path` (hops with join keys, alternatives and cardinality; unreachable/unknown tables reported), `get_business_terms`, and `validate_sql` (parse, read-only and unknown-table checks; typed output schema), plus a `write_sql` workflow prompt. Read-only, no LLM calls, heavy models load lazily; stdio by default, `--transport streamable-http` binds to 127.0.0.1 (no auth). Tools degrade gracefully when no metadata has been ingested. `PromptBuilder.format_business_definitions()` factored out of `format_schema()`. Setup guide in `docs/mcp.md`; `mcp>=2.2,<3` added to `requirements-ci.txt` and a `[mcp]` extra; 26 new tests (#30)
- **OKF concepts in the prompt** — concepts matched during table search are now rendered as a `## Knowledge Notes` block in `PromptBuilder.format_schema()` (after `## Business Definitions`, before `## Database Schema`): title, type, description, resolved tables and the concept body, with the note's own headings demoted to bold. Concepts that point at no known table (e.g. a playbook) are included too. `OKFStore.search_concepts()` runs one ChromaDB query for both uses; `get_okf_tables()` is now a thin wrapper. Import stores a `body` excerpt in concept metadata (bundles imported earlier fall back to the embedded document until re-imported). New `okf.include_in_prompt` / `okf.prompt_chars` config; `okf_hits` returned from `getBuildComponents()`; 9 new tests (#30)
- **OKF bundles in table search** — `MetadataManager/OKFStore.py`: loads an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) v0.2 bundle (Markdown concepts with YAML frontmatter; `index.md`/`log.md`, hidden dirs and non-conforming docs skipped; `deprecated` concepts not indexed) and embeds concepts into an `okf_concepts` ChromaDB collection. Each concept stores candidate table names — from a table concept's `table`/`table_name` key, `resource` URI, title or file name, plus one hop through Markdown links (a Metric linking to `/tables/orders.md` maps to `orders`). `SQLBuilderSupport.__addOKFTables__()` runs after vector table search, resolves candidates against known tables (exact or last dotted segment, so `orders` matches `sales.public.orders`) and appends matches to the direct tables. New `okf` section in `retrieval_config.YAML` (`enabled`, `top_k`, `max_distance`); new authenticated `POST /api/okf/import {path, instance_name}`; 24 new tests (`tests/test_okf_store.py`) (#30)
- **Bug fix — join paths dropped after first unreachable pair** — `kuzuDB.getRelations()` (and legacy `networkxDB.getRelations()`): `break` → `continue`, so a single table pair with no connecting path (or an unknown table) no longer discards the join paths for every remaining pair (#30)
- **Bug fix — role-playing joins collapsed to one edge** — `kuzuDB.getObj()` / `addRelations()`: multiple join conditions between the same two tables (e.g. `order_date_id` / `ship_date_id` → `dim_date`) are kept in a `JoinKeysAlternatives` edge attribute instead of last-write-wins; `getRelations()` emits one relation per alternative so all reach the prompt; empty join keys are dropped when a real condition exists (#30)
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
