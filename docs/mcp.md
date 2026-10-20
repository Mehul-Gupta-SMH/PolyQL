# Use Poly-QL from your agent (MCP)

`backend/mcp_server.py` runs Poly-QL as an [MCP](https://modelcontextprotocol.io/) server, so Claude Code, Codex and other agent frameworks can use its schema knowledge as tools.

It runs in **context mode**. The tools return schema context, and your agent writes the SQL itself. The tools never call an LLM, so the server needs no API key and you don't pay for two models. Every tool is read-only, and Poly-QL never executes queries through MCP.

## Tools

| Tool | What it returns |
|---|---|
| `list_instances()` | Database instances Poly-QL has metadata for (`default` searches all of them) |
| `list_tables(instance)` | Every table in an instance, with its description |
| `search_schema(question, instance)` | Context for answering the question: business definitions, OKF knowledge notes, relevant tables (direct and the intermediate ones needed for joins) with key and relevant columns, and join paths. This is the same context Poly-QL's own SQL generation uses |
| `get_table_schema(table, instance)` | Full column metadata for one table, including source expressions for pipeline-derived tables |
| `find_join_path(tables, instance)` | Each join hop with its condition and cardinality (`1:1`, `1:n`, `n:1`, `n:m`). Lists every alternative when two tables can be joined more than one way, and reports tables that can't be connected |
| `get_business_terms(question, instance)` | Glossary definitions and canonical formulas |
| `validate_sql(sql, instance)` | `{valid, read_only, statement_types, unknown_tables, errors, warnings}`. Checks that the SQL parses, that every statement is read-only, and that FROM/JOIN tables are known. It doesn't run the query or check columns |

The prompt `write_sql(question, instance, dialect)` gives the agent the recommended workflow: `search_schema` → (`get_table_schema` / `find_join_path`) → write → `validate_sql`.

## Install

```bash
pip install "mcp>=2.2,<3"        # or: pip install .[mcp]
```

The server uses the same metadata stores and `Utilities/retrieval_config.YAML` as the web app, so ingest tables first. Without metadata, the listing tools say so, and `validate_sql` skips the unknown-table check.

The embedding model, reranker and ChromaDB load on the first `search_schema` or `get_business_terms` call, not at start-up. Expect the first call to be slow.

## Claude Code

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
python -m backend.mcp_server --transport streamable-http --port 8765
# endpoint: http://127.0.0.1:8765/mcp
claude mcp add --transport http polyql http://127.0.0.1:8765/mcp
```

> **No authentication.** The HTTP server binds to `127.0.0.1` by default and has no auth. Don't pass `--host 0.0.0.0` unless something in front of it handles authentication.

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

- `execute_sql`: running read-only queries with row and time limits, recorded in the outcome store
- `generate_sql`: generate mode, which runs Poly-QL's own LLM pipeline for clients without a model
- Authentication on the HTTP transport
- Resources (`polyql://schema/{instance}/{table}`)

These are tracked in [#30](https://github.com/Mehul-Gupta-SMH/PolyQL/issues/30), section 5.
