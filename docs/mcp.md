# Use Poly-QL from your agent (MCP)

`backend/mcp_server.py` runs Poly-QL as an [MCP](https://modelcontextprotocol.io/) server, so Claude Code, Codex and other agent frameworks can use its schema knowledge as tools.

By default it runs in **context mode**. The tools return schema context, and your agent writes the SQL itself. No default tool calls an LLM, so the server needs no API key and you don't pay for two models. Two tools are opt-in: `execute_sql` runs read-only queries once you configure a connection, and `generate_sql` uses Poly-QL's own LLM pipeline when you pass `--enable-generate`.

## Tools

| Tool | What it returns |
|---|---|
| `list_instances()` | Database instances Poly-QL has metadata for (`default` searches all of them) |
| `list_tables(instance)` | Every table in an instance, with its description |
| `search_schema(question, instance)` | Context for answering the question: business definitions, OKF knowledge notes, relevant tables (direct and the intermediate ones needed for joins) with key and relevant columns, and join paths. This is the same context Poly-QL's own SQL generation uses |
| `get_table_schema(table, instance)` | Full column metadata for one table, including source expressions for pipeline-derived tables |
| `find_join_path(tables, instance)` | Each join hop with its condition and cardinality (`1:1`, `1:n`, `n:1`, `n:m`). Lists every alternative when two tables can be joined more than one way, and reports tables that can't be connected |
| `get_business_terms(question, instance)` | Glossary definitions and canonical formulas |
| `validate_sql(sql, instance)` | `{valid, read_only, statement_types, unknown_tables, unknown_columns, errors, warnings}`. Checks that the SQL parses, that every statement is read-only, that FROM/JOIN tables are known, and that columns exist. Qualified columns (`alias.column`) are checked against their table. Bare columns are checked against the FROM/JOIN tables when every source is a known table, not a CTE or subquery. Aliases, functions and keywords are skipped. It doesn't run the query |
| `get_lineage(table, instance)` | The tables connected to a table in the graph, with join keys |
| `get_derivatives(table, instance)` | Pipeline lineage: which tables a derived table is built from, and which tables are derived from it |
| `execute_sql(sql, instance, max_rows, question)` | **Opt-in.** Runs one read-only `SELECT` and returns `{columns, rows, row_count, truncated, latency_ms, enforced, error}`. See [Running queries](#running-queries-execute_sql) |
| `generate_sql(question, provider, instance, query_type, model)` | **Opt-in, calls an LLM.** Poly-QL's own generation pipeline. See [Generate mode](#generate-mode-generate_sql) |

### Resources

Clients that support MCP resources can attach these as context:

| URI | Content |
|---|---|
| `polyql://tables/{instance}` | Table list with descriptions |
| `polyql://schema/{instance}/{table}` | Full column metadata for one table |
| `polyql://glossary/{term}` | One glossary term with its formula |

The prompt `write_sql(question, instance, dialect)` gives the agent the recommended workflow: `search_schema` → (`get_table_schema` / `find_join_path`) → write → `validate_sql` → (`execute_sql`, if configured).

## Install

```bash
pip install "mcp>=2.2,<3"        # or: pip install .[mcp]
```

The server uses the same metadata stores and `Utilities/retrieval_config.YAML` as the web app, so ingest tables first. Without metadata, the listing tools say so, and `validate_sql` skips the table and column checks with a warning.

The embedding model, reranker and ChromaDB load on the first `search_schema` or `get_business_terms` call, not at start-up. Expect the first call to be slow.

## Claude Code

A ready-made skill teaches the agent the workflow: search, then join paths, then write, then validate, then optionally run. Copy it into your project:

```bash
mkdir -p .claude/skills && cp -r /path/to/PolyQL/docs/agent-skill/polyql-sql .claude/skills/
```

For other agents (Codex, an `AGENTS.md`), paste the **Workflow** section of [`docs/agent-skill/polyql-sql/SKILL.md`](agent-skill/polyql-sql/SKILL.md) into your instructions.


```bash
claude mcp add polyql -- python /path/to/PolyQL/backend/mcp_server.py
```

Add `--scope project` to write it to the repo's `.mcp.json` and share it with your team, or `--scope user` to make it available in every project. The equivalent `.mcp.json`:

```json
{
  "mcpServers": {
    "polyql": {
      "command": "python",
      "args": ["/path/to/PolyQL/backend/mcp_server.py"]
    }
  }
}
```

Use the Python interpreter from Poly-QL's virtualenv if it has one, e.g. `/path/to/PolyQL/venv/bin/python`.

## Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.polyql]
command = "python"
args = ["/path/to/PolyQL/backend/mcp_server.py"]
```

## Over HTTP (shared or remote agents)

```bash
export POLYQL_MCP_TOKEN="$(openssl rand -hex 32)"
python -m backend.mcp_server --transport streamable-http --port 8765
# endpoint: http://127.0.0.1:8765/mcp
claude mcp add --transport http polyql http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $POLYQL_MCP_TOKEN"
```

**Authentication.** Requests must send `Authorization: Bearer <token>` when either of these is set:

- `POLYQL_MCP_TOKEN`: a shared secret, compared in constant time.
- `JWT_SECRET`: also accept Poly-QL login tokens from `POST /auth/login`. The web app must run with the same `JWT_SECRET`; otherwise each process generates its own random secret and the tokens won't match.

Without either, the server runs with no authentication and logs a warning. That's only allowed on `127.0.0.1`, `localhost` or `::1`. For any other `--host` the server refuses to start unless authentication is configured, or you pass `--insecure-no-auth` explicitly.

## Running queries (`execute_sql`)

`execute_sql` only works when the operator configures a connection through environment variables. An agent can never pass a connection string itself:

```bash
export POLYQL_MCP_DB_SALES="postgresql://readonly_user:...@db/sales"   # instance "sales"
export POLYQL_MCP_DB="sqlite:////data/warehouse.db"                    # fallback for any instance
export POLYQL_MCP_MAX_ROWS=1000     # server cap on rows returned (default 1000)
export POLYQL_MCP_TIMEOUT_S=30      # statement timeout in seconds (default 30)
```

The instance name is upper-cased, with non-alphanumeric characters turned into `_`, so `sales-eu` becomes `POLYQL_MCP_DB_SALES_EU`.

Safeguards:

- **Before connecting**, the SQL must be exactly one statement, and `sqlparse` must classify it as a `SELECT`. `WITH … DELETE` is rejected, and so is `SELECT … INTO`, which writes a table in PostgreSQL or a file with MySQL's `INTO OUTFILE`.
- **Always rolled back.** The transaction is never committed.
- **Per-database protections**, listed in the result's `enforced` field:

  | Database | Read-only | Timeout |
  |---|---|---|
  | PostgreSQL | `SET TRANSACTION READ ONLY` | `statement_timeout` |
  | MySQL | `SET SESSION TRANSACTION READ ONLY` | `max_execution_time` (applies to `SELECT`) |
  | MariaDB | `SET SESSION TRANSACTION READ ONLY` | `max_statement_time` |
  | SQLite | `PRAGMA query_only` | Interrupted via a progress handler |
  | Others | (rollback only) | Not enforced |

- **Row cap:** results are limited to `max_rows`, which can't exceed `POLYQL_MCP_MAX_ROWS`. `truncated` tells you whether more rows existed.
- **No credentials in errors:** the connection string and any `user:password@` are removed from error messages.
- **Recorded:** every run is logged to the outcome store (`validation/outcome_store`) with `provider="mcp"` and the optional `question`.

> **Use a database user with read-only grants.** The checks above are layers of defence, not a substitute for database permissions.

## Generate mode (`generate_sql`)

```bash
python -m backend.mcp_server --enable-generate      # or POLYQL_MCP_ENABLE_GENERATE=1
```

This adds `generate_sql`, which runs Poly-QL's full pipeline (adaptive retrieval and the LLM call) with the provider and API key from Poly-QL's config. It costs tokens. It's useful for clients without a capable model, or for comparing Poly-QL's output with your agent's. The result is `{type, content, error}`, where `type` is `sql`, `code` or `clarify`; for `clarify`, `content` is Poly-QL's question. Avoid `provider="claude_code"` from inside Claude Code, because it starts a nested Claude Code session.

## Agent frameworks

These follow each framework's documented MCP support; check its docs for the current API.

**Claude Agent SDK (Python)**

```python
from claude_agent_sdk import ClaudeAgentOptions, query

options = ClaudeAgentOptions(
    mcp_servers={"polyql": {"type": "stdio", "command": "python",
                            "args": ["/path/to/PolyQL/backend/mcp_server.py"]}},
    allowed_tools=["mcp__polyql__search_schema", "mcp__polyql__find_join_path",
                   "mcp__polyql__get_table_schema", "mcp__polyql__validate_sql"],
)
async for message in query(prompt="Monthly revenue by region for 2025?", options=options):
    print(message)
```

**OpenAI Agents SDK**

```python
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

async with MCPServerStdio(params={"command": "python",
                                  "args": ["/path/to/PolyQL/backend/mcp_server.py"]}) as polyql:
    agent = Agent(name="analyst", instructions="Answer with SQL.", mcp_servers=[polyql])
    result = await Runner.run(agent, "Monthly revenue by region for 2025?")
```

**LangGraph** (`langchain-mcp-adapters`)

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({"polyql": {"transport": "stdio", "command": "python",
                                          "args": ["/path/to/PolyQL/backend/mcp_server.py"]}})
tools = await client.get_tools()
```

## Not yet available

- Column checks inside CTEs and subqueries in `validate_sql`
- Read-only enforcement beyond rollback on databases other than PostgreSQL, MySQL/MariaDB and SQLite
- Measured cold-start time on a full install

These are tracked in [#30](https://github.com/Mehul-Gupta-SMH/PolyQL/issues/30), section 5.
