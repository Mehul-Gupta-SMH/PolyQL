# CLAUDE.md — Poly-QL

Guidelines and context for AI-assisted development on this project.

---

## Git Workflow

- **Never push directly to `master`**
- Use branch `Claude/Playground/Dev` (or create a feature branch)
- Commit and push branch only; let the user merge via GitHub PR

---

## Project Identity

- App name: **Poly-QL** (header/branding)
- Repo/package names still say `SQLCoder` — do not rename those

---

## Backend

- Entry point: `backend/app.py` (FastAPI)
- Business logic: `main.py` (`gatherRequirements`, `generateQuery`, `generate_pipeline_dict`)
- Balance checks: `backend/balance.py` — concurrent, one checker per provider
- Run: `uvicorn backend.app:app --reload` from project root
- MCP server: `python -m backend.mcp_server` (stdio) or `--transport streamable-http` (bearer auth via `POLYQL_MCP_TOKEN` / `JWT_SECRET`; refuses non-local hosts without auth). Context tools never call an LLM; `execute_sql` needs `POLYQL_MCP_DB[_<INSTANCE>]` and is read-only; `generate_sql` needs `--enable-generate`. See `docs/mcp.md`. Uses MCP SDK v2 (`from mcp.server.mcpserver import MCPServer`; v1 `FastMCP` no longer exists)

## Frontend

- Vite + React (`frontend/`)
- Run: `npm run dev` from `frontend/`
- Sessions stored in `localStorage` under `poly_ql_sessions` (max 30)

---

## Key Conventions

- **Code fence regex** for LLM responses: `r'```(?:\w+)?\s*(.*?)\s*```'`
- **generateQuery signature**: `(userQuery, LLMservice, query_type, conversation=None)`
- **Balance checker return shape**: `{balance, currency, available, status, label}`
  - `available: False` greys out the provider in the UI
  - `status` values: `ok | no_balance | invalid_key | unavailable | no_config | error`
- **OpenAI secret keys** (`sk-...`) cannot access `/dashboard/billing/credit_grants` — returns 403.
  The checker falls back to `/v1/models` for key validity and returns `label: "N/A"`.
- **Claude Code provider** uses `subprocess.run(["claude", "-p", ...])` — no API key, uses local CLI session.
  Balance check runs `claude --version`.

---

## Release Notes

- Maintain `CHANGELOG.md` in the project root following [Keep a Changelog](https://keepachangelog.com/) format
- Current version: **0.0.1** (released 2026-03-21)
- Add an `[Unreleased]` section at the top for work-in-progress; move to a versioned section on release
- Per-release highlight summaries go in `docs/release/vX.Y.Z.md`

---

## Testing

- Test suite lives in `tests/` — 193 tests as of 2026-03-21
- `conftest.py` pre-mocks heavy ML packages (torch, chromadb, sentence-transformers, FlagEmbedding, sqlalchemy, pyvis)
- `requirements-ci.txt` installs the lightweight runtime deps CI needs (requests, kuzu, PyJWT, etc.)
- Run all tests: `pytest tests/` (an autouse fixture clears the retrieval cache and shared models between tests)
- Lint: `python -m ruff check backend/ tests/ validation/` (runs in CI before tests)

## Observability

- `backend/logging_config.py` — `configure_logging()` switches root logger to JSON format on startup
- `backend/metrics.py` — in-memory Prometheus counters; `GET /metrics` exposes them
- Every HTTP request is logged as a JSON line with `method`, `path`, `status`, `latency_ms`
- LLM calls in `/api/chat` and `/api/chat/stream` are counted per provider

---

## Change Log

| Date | File | Change |
|------|------|--------|
| 2026-03-07 | `backend/balance.py` | Fixed OpenAI `credit_grants` URL (`/dashboard/` → `/v1/dashboard/`); added key-validity fallback via `/v1/models` when balance endpoints return 403 |
| 2026-03-07 | `backend/usage_tracker.py` | New: thread-safe in-memory token usage tracker for Claude Code CLI |
| 2026-03-07 | `APIManager/AllAPICaller.py` | Claude Code: switched to `--output-format json`, records token usage |
| 2026-03-07 | `main.py` | `gatherRequirements`: re-prompt on non-JSON LLM response; extract + return `options` array |
| 2026-03-07 | `taskRequirementGather.txt` | Added `options` array to clarify format; enforced JSON-only output |
| 2026-03-07 | `backend/app.py` | Pass `options` through in clarify response |
| 2026-03-07 | `frontend/…/ChatInterface.jsx` | Retry strips assistant clarify history; `handleOptionSelect` for pill button clicks |
| 2026-03-07 | `frontend/…/ChatMessage.jsx` | Clarify bubble renders clickable option pill buttons |
| 2026-03-13 | `APIManager/AllAPICaller.py` | P1: Added `CallServiceStream()` generator — SSE token streaming for OpenAI/Codex/GROQ/Anthropic/Google; CLI providers fall back to batch |
| 2026-03-13 | `main.py` | P1: Added `generateQueryStream()` generator — yields `{event:token}` chunks + final `{event:done}` with validated result |
| 2026-03-13 | `backend/app.py` | P1: New `POST /api/chat/stream` SSE endpoint — Phase 1 (gather) runs sync, Phase 2 streams tokens; clarify returns single done event |
| 2026-03-13 | `frontend/…/ChatInterface.jsx` | P1: `_callApiStream()` — fetch ReadableStream SSE consumer; streaming bubble updated token-by-token, finalised on done event |
| 2026-03-13 | `frontend/…/ChatMessage.jsx` | P1: New `streaming` bubble type — dark code block with `●●●` placeholder while tokens arrive |
| 2026-03-13 | `main.py` | R1: `_adaptive_retrieval()` + `_is_retrieval_confident()` — LLM rewrites ChromaDB search query when retrieval is weak; up to 3 rounds, stagnation/empty/failure early exits; replaces 2-line fallback in `generateQuery` and `generateQueryStream` |
| 2026-03-13 | `Utilities/retrieval_config.YAML` | R1: Added `re_retrieval` config section: `max_rounds`, `min_direct_tables`, `rewrite_provider` |
| 2026-03-13 | `tests/test_adaptive_retrieval.py` | R1: 9 new tests covering confident-first-round, rewrite-improves, stagnation, empty-schema, rewriter-exception |
| 2026-03-13 | `tests/test_kuzu.py` | Fix: test isolation — patch `_kuzu_base_dir` directly and clear `_DB_POOL`/`_SCHEMA_READY` in migration test to prevent lru_cache poisoning across test runs |
| 2026-03-21 | `TASK.md`, `docs/` | Collated R1_PLAN.md + joblog.md into TASK.md (R1 design notes + session history); deleted source files; created docs/arch_system.md, arch_query_flow.md, arch_ingest_flow.md, arch_storage.md with Mermaid diagrams |
| 2026-03-16 | `validation/outcome_store.py`, `backend/app.py`, `tests/test_outcome_store.py` | QT1: outcome recorder — appends `{session_id, query_id, nl_query, generated_sql, provider, outcome, error_type, row_count, latency_ms}` to `outcomes.jsonl` + SQLite after every `/api/execute` |
| 2026-03-16 | `frontend/src/components/ChatMessage.jsx`, `ChatInterface.jsx` | QT2: outcome badge (✓/○/✕) inline with Execute button; session-pane dot (green/amber/red) next to session title |
| 2026-03-20 | `backend/logging_config.py`, `backend/metrics.py`, `backend/app.py` | B3: JSON structured logging + Prometheus metrics — `GET /metrics`, request middleware, LLM call counters |
| 2026-03-20 | `tests/test_failure_scenarios.py` | T2: 16 failure-scenario tests — LLM timeout, 429, malformed/empty response, ConnectionError, executor errors |
| 2026-03-20 | `.github/workflows/ci.yml`, `requirements-ci.txt` | CI2: ruff lint step added to CI; `ruff check backend/ tests/ validation/` runs before tests on every PR |
| 2026-03-20 | `backend/ingestion.py`, `tests/test_ingestion.py` | C4: `database.schema.table` qualified name support — 3 regex patterns in `parse_pipeline` updated; 24 new tests |
| 2026-03-22 | `MetadataManager/GlossaryStore.py` | SL0+SL1: `business_terms` SQLite table + CRUD (`add_term`, `get_term`, `update_term`, `delete_term`, `list_terms`, `search_by_name`) + `index_term()` ChromaDB embed + `get_business_context()` semantic search |
| 2026-03-22 | `main.py` | SL2: `_get_business_context()` wrapper; called in `generateQuery`, `generateQueryStream`, `gatherRequirements`; attaches `glossary_hits` to context dict before `format_schema` |
| 2026-03-22 | `APIManager/PromptBuilder.py` | SL2: `format_schema()` renders `## Business Definitions` block from `context['glossary_hits']` — term name, formula, table deps, example — prepended before schema section |
| 2026-03-22 | `backend/glossary.py`, `backend/app.py` | SL3: Glossary REST API (single/bulk POST, GET, PUT, DELETE, lexical search); CORS updated to allow PUT |
| 2026-03-22 | `tests/test_glossary_store.py`, `tests/test_glossary_retrieval.py` | SL6: 35 CRUD tests + 19 semantic/injection tests; all 247 suite tests pass |
| 2026-10-18 | `MetadataManager/MetadataStore/relationdb/kuzuDB.py`, `networkxDB.py`, `tests/test_kuzu.py` | #30: `getRelations` skips unreachable pairs instead of aborting; role-playing joins preserved via `JoinKeysAlternatives` edge attribute; 3 new tests (250 total) |
| 2026-10-18 | `MetadataManager/OKFStore.py`, `SQLBuilderComponents.py`, `backend/okf.py`, `Utilities/retrieval_config.YAML`, `tests/test_okf_store.py` | #30: OKF bundle import + `okf_concepts` collection; table search adds tables that matching concepts describe or link to (`__addOKFTables__`); `POST /api/okf/import`; 24 new tests (274 total) |
| 2026-10-18 | `MetadataManager/OKFStore.py`, `SQLBuilderComponents.py`, `APIManager/PromptBuilder.py`, `Utilities/retrieval_config.YAML`, `tests/test_okf_store.py` | #30: matched OKF concepts rendered as `## Knowledge Notes` in `format_schema()`; `search_concepts()` + `tables_from_concepts()`; `okf_hits` in context; `okf.include_in_prompt` / `prompt_chars`; 9 new tests (283 total) |
| 2026-10-19 | `backend/mcp_server.py`, `APIManager/PromptBuilder.py`, `docs/mcp.md`, `requirements-ci.txt`, `pyproject.toml`, `tests/test_mcp_server.py` | #30: MCP server (context mode) — 7 read-only tools + `write_sql` prompt; `format_business_definitions()` factored out; 26 new tests (309 total) |
| 2026-10-20 | `backend/mcp_server.py`, `backend/executor.py`, `docs/mcp.md`, `requirements-ci.txt`, `tests/test_mcp_server.py` | #30: MCP `execute_sql` (read-only, server-side connections, row/time limits, redaction), opt-in `generate_sql`, HTTP bearer auth, `get_lineage`/`get_derivatives`, resources, column checks in `validate_sql`, `SELECT … INTO` blocked; 31 new tests (340 total) |
| 2026-10-20 | `backend/mcp_server.py`, `backend/executor.py`, `docs/agent-skill/`, `docs/mcp.md`, `tests/test_mcp_server.py` | #30: bare-column checks in `validate_sql`; MySQL/MariaDB read-only transaction; MariaDB timeout fix (`max_statement_time`); agent skill; 7 new tests (347 total) |
| 2026-10-20 | `MetadataManager/MetadataStore/relationdb/joinpath.py`, `kuzuDB.py`, `ManageRelations.py`, `SQLBuilderComponents.py`, `APIManager/PromptBuilder.py`, `backend/app.py`, `backend/ingestion.py`, `backend/mcp_server.py`, `frontend/…/DataLineage.jsx` | #30: cardinality-weighted joins, one Steiner tree per query, `Kind` fk/lineage edges (lineage never joined), alternatives in `/api/joinpath` + Join Path UI, row-multiplication warning in prompt; 7 new tests (354 total) |
| 2026-10-20 | `MetadataManager/OssieStore.py`, `backend/semantic.py`, `backend/ingestion.py`, `relationdb/joinpath.py`, `docs/ossie.md`, `tests/test_ossie.py` | #30: Apache Ossie import/export (datasets/relationships/metrics), `store_table(vdb=, replace=)`, qualified names in join keys; 34 new tests (388 total) |
| 2026-10-20 | `MetadataManager/OKFStore.py`, `backend/okf.py`, `frontend/…/KnowledgePanel.jsx`, `App.jsx`, `tests/test_okf_store.py` | #30: Knowledge tab (OKF/Ossie import + export), OKF bundle list/remove, re-import replaces a bundle, OKF data-dictionary export (zip); 13 new tests (401 total) |
| 2026-10-20 | `benchmark/run_inference.py`, `benchmark/compare.py`, `benchmark/mcp_coldstart.py`, `tests/test_benchmark_tools.py` | #30: `--without okf,glossary,evidence` ablations, run comparison with McNemar test, MCP cold-start timer; 12 new tests (413 total) |
| 2026-10-20 | `backend/glossary.py`, `MetadataManager/GlossaryStore.py`, `frontend/…/BusinessGlossary.jsx`, `frontend/src/utils/glossaryImport.js`, `App.jsx`, `tests/test_glossary_api.py` | SL4: Glossary tab (search, add/edit/delete, CSV/JSON import, re-index); API now authenticated, embeds on create/update/import and unindexes on delete, `POST /reindex`, bulk upsert by name, 409 on duplicate names; 13 new tests (426 total) + 8 Vitest tests |
| 2026-10-20 | `.github/workflows/ci.yml`, `frontend/src/App.test.jsx`, `frontend/…/__tests__/QueryInput.test.jsx`, `SQLResult.test.jsx`, `ChatInterface.jsx` | CI3: frontend job in CI (vitest + build); stale frontend tests updated to the current UI (64 pass); chat 401 now logs out instead of showing an error |
| 2026-10-20 | `MetadataManager/MetadataStore/shared_models.py`, `MetadataManager/retrieval_cache.py`, `RAGPipeline.py`, `GlossaryStore.py`, `OKFStore.py`, `ManageRelations.py`, `importData.py`, `backend/ingestion.py`, `backend/okf.py`, `main.py`, `retrieval_config.YAML`, `tests/test_retrieval_cache.py` | P2: models/Chroma client loaded once per process; TTL+LRU cache of `getRelevantContext()` cleared on every metadata write (generation guard against writes mid-search); 32 new tests (457 total) |
| 2026-10-23 | `frontend/src/components/QueryInput.jsx`, `SQLResult.jsx`, `QueryHistory.jsx` (+ tests), `MetadataManager/GlossaryStore.py` | Cleanup: removed unused pre-chat components and their tests; fixed F841 in `get_business_context` |
| 2026-10-23 | `backend/app.py`, `frontend/…/SchemaERD.jsx`, `TableNode.jsx`, `RelationEdge.jsx`, `TermNode.jsx`, `frontend/src/utils/erdModel.js`, `tests/test_schema_api.py` | U6+SL5: `/api/schema` relations carry alternatives, cardinality and kind (no more pair-merging); ERD shows cardinality labels, n:m in amber, dashed lineage, legend, optional business-term nodes; 8 new tests (465 total) + 19 Vitest tests |
