# Poly-QL

**Poly-QL** is an AI-powered data assistant that turns plain-English questions into SQL, Spark SQL, or PySpark code — and keeps track of your database schema so you don't have to paste it every time.

Ask *"Which customers placed the most orders last quarter?"* and get a ready-to-run query in seconds.

---

## Demo

### Natural language → SQL (with clarifying questions)

<video src="demo/pages/01_query_chat.webm" controls width="100%"></video>

> Ask a complex multi-table question. Poly-QL asks one clarifying question, then streams the full SQL back token by token.

---

### Schema / ERD explorer

<!-- PLACEHOLDER: run `python demo/record_pages.py --only 02`, upload demo/pages/02_schema_erd.webm to a GitHub issue comment to get a CDN URL, then replace this comment with:  <video src="https://github.com/user-attachments/assets/YOUR_ID" controls width="100%"></video> -->
*Demo coming soon — run `python demo/record_pages.py --only 02` to generate.*

> Interactive entity-relationship diagram of your entire schema. Click any table to open its data dictionary.

---

### Join Path explorer

<!-- PLACEHOLDER: run `python demo/record_pages.py --only 03` -->
*Demo coming soon — run `python demo/record_pages.py --only 03` to generate.*

> Select any two tables and instantly visualise the shortest JOIN route with join keys and cardinality annotations.

---

### Ingest Table — auto-document a pipeline

<!-- PLACEHOLDER: run `python demo/record_pages.py --only 04` -->
*Demo coming soon — run `python demo/record_pages.py --only 04` to generate.*

> Paste an `INSERT INTO … SELECT` and the LLM auto-generates a full data dictionary for the output table.

---

## Features

| | |
|---|---|
| **Multi-turn chat** | The assistant asks clarifying questions before generating a query, so you always get something correct rather than something fast |
| **Schema-aware** | Automatically retrieves relevant tables, JOIN paths, and column descriptions from your stored schema |
| **Multi-provider** | OpenAI (GPT-4o), Anthropic (Claude 3.5 / 4), Google (Gemini 2.0), Groq, OpenAI Codex, Claude Code CLI |
| **Model selection** | Pick the specific model per request — switch between GPT-4o and GPT-4o-mini in the toolbar |
| **Multiple output modes** | SQL · Spark SQL · PySpark DataFrame API · Pandas |
| **Schema / ERD viewer** | Interactive entity-relationship diagram of your entire schema |
| **Ingest Table** | Paste a pipeline SQL (`INSERT INTO … SELECT` or CTAS) and let the LLM auto-document the output table |
| **Multi-database instances** | Tag tables with a database type (Snowflake, Databricks, MS SQL, etc.) and a named instance — query each instance independently |
| **Join Path explorer** | Select any two tables and instantly see the shortest JOIN route, with join keys and cardinality annotations |
| **Derivative Tables** | See which tables were built from a source table via pipeline SQL, and which tables a derived table originates from |
| **Qualified table names** | Full `database.schema.table` notation supported in pipeline ingestion — qualifiers are preserved through parsing and stored correctly |
| **Business Semantic Layer** | Define business terms (KPIs, formulas, synonyms) in a glossary; terms are embedded and injected into the LLM prompt when semantically relevant — ensures canonical definitions are always used |
| **Execution outcome tracking** | Every query run via the UI records a success / empty / failure verdict with latency and error details — powers the future validation layer |
| **Benchmark evaluation** | BIRD and Spider text-to-SQL benchmark harness — ingest, run batch inference, compute EX and VES metrics per difficulty and per DB |
| **Observability** | JSON-structured request logs and a `GET /metrics` Prometheus endpoint for request latencies, error rates, and LLM call counts per provider |
| **User accounts** | Login / register with username + password, or **Sign in with Google** |
| **Session history** | Conversations are saved per user and accessible across sessions |

---

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/Mehul-Gupta-SMH/PolyQL.git
cd PolyQL

python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
```

### 2. Set your API keys

The API key file is gitignored — create it locally:

**`APIManager/model_access_config.YAML`**

Add only the providers you have keys for:

```yaml
OPEN_AI:
  api_key: sk-...
  model_name: gpt-4o-mini
  api_template: APIManager/APIHeads/OPEN_AI.json

ANTHROPIC:
  api_key: sk-ant-...
  model_name: claude-3-5-haiku-20241022
  api_template: APIManager/APIHeads/ANTHROPIC.json

GOOGLE:
  api_key: AIza...
  model_name: gemini-2.0-flash
  api_template: APIManager/APIHeads/GOOGLE.json

GROQ:
  api_key: gsk_...
  model_name: gemma-7b-it
  api_template: APIManager/APIHeads/GROQ.json

CODEX:
  api_key: sk-...           # OpenAI key (Codex uses the OpenAI endpoint)
  model_name: o4-mini
  api_template: APIManager/APIHeads/CODEX.json

CLAUDE_CODE:
  api_key: ""               # No key needed — uses the local claude CLI
  model_name: claude-sonnet-4-5
  api_template: APIManager/APIHeads/CLAUDE_CODE.json
```

### 3. Load your schema

Before the assistant can generate queries it needs to know your database structure.

**a) Import table metadata** (one JSON file per table — see [Schema Format](#schema-format)):

```python
from MetadataManager.MetadataBuilder.importExisting.importData import importDD

imp = importDD()
imp.importData("path/to/orders.json")
imp.importData("path/to/customers.json")
```

**b) Define JOIN relationships:**

```python
from MetadataManager.MetadataStore.ManageRelations import Relations

rel = Relations()   # default backend: Kuzu embedded graph DB
rel.addRelation([
    ("orders", "customers",   ["orders.customer_id = customers.customer_id"]),
    ("orders", "order_items", ["orders.order_id = order_items.order_id"]),
])
```

> **Multi-instance:** Pass `instance_name` to scope metadata to a named DB instance:
> ```python
> imp = importDD(instance_name="prod_snowflake", db_type="snowflake")
> rel = Relations(instance_name="prod_snowflake")
> ```
> All API endpoints (`/api/chat`, `/api/schema`, `/api/joinpath`, etc.) also accept `?instance_name=` as a query parameter.

### 4. Start the app

**Backend** (FastAPI, port 8000):
```bash
venv/Scripts/uvicorn backend.app:app --reload --port 8000
```

**Frontend** (Vite + React, port 5173):
```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**, register an account, and start querying.

---

## Providers & Models

Use the toolbar dropdowns in the Chat tab to choose a provider and model per session.

| Provider | Available models |
|---|---|
| OpenAI | GPT-4o mini · GPT-4o · GPT-4.1 · GPT-3.5 Turbo |
| OpenAI Codex | o4-mini · o3-mini · o3 · o1 |
| Anthropic | Claude 3.5 Haiku · Claude 3.5 Sonnet · Claude Sonnet 4.6 · Claude Opus 4.6 |
| Google Gemini | Gemini 2.0 Flash · Gemini 2.0 Flash Lite · Gemini 1.5 Pro · Gemini 1.5 Flash |
| GROQ | Gemma 7B · Llama 3.3 70B · Llama 3.1 8B · Mixtral 8x7B |
| Claude Code | Claude Sonnet 4.5 · Claude Sonnet 4.6 · Claude Opus 4.6 · Claude Haiku 4.5 |

> **Claude Code** requires the [Claude Code CLI](https://claude.ai/code) installed and authenticated locally:
> ```bash
> npm install -g @anthropic-ai/claude-code
> claude login
> ```

---

## Google SSO (optional)

1. Create an OAuth 2.0 client in [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Add `http://localhost:8000/auth/google/callback` as an authorised redirect URI
3. Set environment variables before starting the backend:

```bash
export GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
export GOOGLE_CLIENT_SECRET=your-client-secret
```

The **Sign in with Google** button appears on the login page automatically when these variables are set.

---

## Business Semantic Layer

Add business definitions that get automatically injected into the LLM context when relevant:

```bash
# Add a term via REST API
curl -X POST http://localhost:8000/api/glossary/terms/single \
  -H "Content-Type: application/json" \
  -d '{
    "term_name": "ARR",
    "full_name": "Annual Recurring Revenue",
    "definition": "Sum of all recurring subscription revenue normalised to a 12-month period.",
    "formula": "SUM(monthly_revenue * 12) WHERE subscription_type = '\''recurring'\''",
    "table_deps": ["subscriptions", "invoices"],
    "example_value": "4200000"
  }'
```

Terms are embedded into ChromaDB. When a user asks about "ARR" or "recurring revenue", the matching terms are prepended to the schema section so the LLM uses the canonical formula.

---

## Schema Format

One JSON file per table:

```json
{
  "tableName": "orders",
  "tableDesc": "Records every customer order placed through the platform.",
  "records": [
    {
      "TableName":     "orders",
      "ColumnName":    "order_id",
      "DataType":      "INT",
      "Constraints":   "PRIMARY KEY",
      "Desc":          "Unique identifier for each order.",
      "logic":         "",
      "type_of_logic": "",
      "base_table":    ""
    },
    {
      "TableName":     "orders",
      "ColumnName":    "customer_id",
      "DataType":      "INT",
      "Constraints":   "FOREIGN KEY",
      "Desc":          "References the customer who placed the order.",
      "logic":         "",
      "type_of_logic": "",
      "base_table":    ""
    }
  ]
}
```

> **Tip:** For pipeline / derived tables, use the **Ingest Table** tab in the UI instead. Paste an `INSERT INTO … SELECT` statement and the assistant will auto-generate the data dictionary for you.

---

## Benchmark Evaluation

Run Poly-QL against the BIRD or Spider text-to-SQL benchmarks to measure accuracy.

### BIRD mini-dev (500 questions)

```bash
# 1. Ingest all 11 BIRD databases into PolyQL metadata
python -m benchmark.ingest_bird \
    --data_dir benchmark/bird_data/minidev/MINIDEV \
    --instance_prefix bird

# 2. Run batch inference
python -m benchmark.run_inference \
    --benchmark bird \
    --data_dir benchmark/bird_data/minidev/MINIDEV \
    --provider claude_code \
    --output_file results/bird_mini.jsonl \
    --instance_prefix bird \
    --sleep 1

# 3. Evaluate EX + VES
python -m benchmark.evaluate \
    --results_file results/bird_mini.jsonl \
    --benchmark bird \
    --data_dir benchmark/bird_data/minidev/MINIDEV \
    --output_csv results/bird_mini_summary.csv
```

### Universal ingestion agent (any benchmark format)

For benchmarks in formats other than BIRD/Spider, use the LLM-powered ingestion agent — it auto-detects the schema format (tables.json, SQL CREATE TABLE files, CSV metadata, custom JSON/YAML):

```bash
export ANTHROPIC_API_KEY=sk-ant-...

python -m benchmark.ingest_agent \
    --data_dir /path/to/any/benchmark \
    --instance_prefix mybench \
    [--db_ids db1,db2] \
    [--force]
```

---

## Configuration

### Provider balance & availability

`GET /api/providers/balance` checks credit balance for every configured provider and returns availability labels shown in the UI dropdowns. Providers with invalid keys or no credit are greyed out automatically.

### Tuning the requirement-gathering loop

The assistant fetches table schemas in an agentic loop before generating a query. The max number of schema-fetch calls per turn is configurable in `Utilities/retrieval_config.YAML`:

```yaml
gather_requirements:
  max_tool_calls: 5   # increase for large schemas; decrease for faster (less thorough) responses
```

---

## Running Tests

```bash
python -m pytest tests/ -v
```

247 tests, no API keys or ML models required — all external dependencies are mocked in `tests/conftest.py`.

CI also runs a lint check before tests:

```bash
python -m ruff check backend/ tests/ validation/
```

---

## Project Layout

```
SQLCoder/
├── main.py                    # Core logic: generateQuery, gatherRequirements, _preload_schemas_bulk
├── SQLBuilderComponents.py    # RAG retrieval pipeline orchestration
├── backend/
│   ├── app.py                 # FastAPI application + all endpoints
│   ├── auth.py                # JWT auth, user accounts, sessions (SQLite)
│   ├── balance.py             # Provider credit/availability checker (GET /api/providers/balance)
│   ├── glossary.py            # Business Semantic Layer REST API (CRUD + search)
│   ├── ingestion.py           # Pipeline SQL parsing + store_table (SQLite + ChromaDB + Kuzu)
│   ├── logging_config.py      # JSON structured log formatter (configure_logging)
│   └── metrics.py             # In-memory Prometheus counters (GET /metrics)
├── frontend/
│   └── src/
│       ├── App.jsx
│       ├── components/        # ChatInterface, SchemaERD, IngestTable, DataLineage, LoginPage
│       ├── contexts/          # AuthContext (JWT + Google SSO)
│       └── constants/         # providerLabels.js (provider & model catalog)
├── APIManager/
│   ├── AllAPICaller.py        # Multi-provider HTTP + subprocess LLM client
│   ├── PromptBuilder.py       # Prompt templates + schema formatter (injects Business Definitions)
│   └── APIHeads/              # Per-provider JSON request templates
├── MetadataManager/
│   ├── GlossaryStore.py       # Business term CRUD + ChromaDB embedding + semantic search
│   └── MetadataStore/
│       ├── relationdb/
│       │   ├── kuzuDB.py      # Kuzu embedded graph DB — auto-migrates from Relations.pickle
│       │   └── networkxDB.py  # Legacy NetworkX backend (still available)
│       └── vdb/               # ChromaDB vector store abstraction
├── Utilities/                 # Config loader, SQLite CRUD, YAML configs
├── benchmark/
│   ├── _bench_utils.py        # Shared helpers: EX metric, JSONL I/O, instance management
│   ├── ingest_bird.py         # BIRD benchmark ingestion (tables.json + CSV descriptions)
│   ├── ingest_spider.py       # Spider benchmark ingestion
│   ├── ingest_agent.py        # LLM-powered universal ingestion agent (any format)
│   ├── run_inference.py       # Batch inference runner (resume-safe JSONL)
│   ├── evaluate.py            # EX + VES evaluation with per-difficulty/db breakdown
│   └── sample_data/           # 4-table music_store DB + 8 sample questions
├── validation/
│   ├── outcome_store.py       # Records query execution outcomes to JSONL + SQLite
│   └── corpus/                # Runtime store — gitignored
├── results/                   # Benchmark result files — gitignored
└── tests/                     # pytest suite (247 tests)
```

For architecture deep-dives and extension guides see **[DEVELOPER.md](DEVELOPER.md)**.

---

## License

MIT — see [LICENSE](LICENSE) for details.
