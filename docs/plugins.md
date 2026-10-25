# Install Poly-QL as an agent plugin

Poly-QL ships as a **Claude Code plugin** and has a **Codex** setup, so an agent gets the MCP server, the `polyql-sql` skill and three commands in one step. The tools themselves are described in [mcp.md](mcp.md).

## What you get

| Piece | Where | What it does |
|---|---|---|
| MCP server `polyql` | `plugins/polyql/.claude-plugin/plugin.json` | Runs `backend/mcp_server.py` through the launcher below |
| Skill `polyql-sql` | `plugins/polyql/skills/polyql-sql/SKILL.md` | The workflow: search, join paths, write, validate, optionally run |
| `/polyql:sql <question>` | `plugins/polyql/commands/sql.md` | Answer a data question with SQL |
| `/polyql:schema <table or topic>` | `plugins/polyql/commands/schema.md` | Columns, lineage and neighbours of a table |
| `/polyql:explain-join <t1> <t2> ...` | `plugins/polyql/commands/explain-join.md` | Join hops, cardinality and alternatives |

## Before you install

The plugin does not bundle Poly-QL's Python code or its models. The MCP server runs from a Poly-QL checkout, so you need:

```bash
git clone https://github.com/Mehul-Gupta-SMH/PolyQL
cd PolyQL
python -m venv .venv && .venv/bin/pip install ".[mcp]"
```

and some ingested metadata (see the main readme), because the server reads the same stores as the web app.

## The launcher

`plugins/polyql/bin/polyql-mcp` starts the server. It finds Poly-QL and Python like this:

| Setting | Order of lookup |
|---|---|
| Poly-QL checkout | `POLYQL_HOME`, then the clone the script sits in (`plugins/polyql/bin/` is three levels below the root) |
| Python | `POLYQL_PYTHON`, then `<checkout>/.venv`, `<checkout>/venv`, `$VIRTUAL_ENV`, `python3`, `python` |

When Claude Code installs the plugin from a marketplace it copies `plugins/polyql/` into its cache, so the clone is no longer next to the script. Set `POLYQL_HOME` (the plugin option **Poly-QL checkout**, below) in that case. Used straight from a clone, nothing needs setting.

Run `plugins/polyql/bin/polyql-mcp --help` to check it. Extra arguments go to the server, so `--transport streamable-http` works too. `POLYQL_MCP_ENABLE_GENERATE=1` (or `true`) adds `--enable-generate`.

There is also a `polyql-mcp` console script: `pip install -e ".[mcp]"` from a checkout puts it on `PATH` (editable install — the server imports top-level modules such as `main.py` that a regular wheel leaves out). It runs `backend.mcp_server:main` directly and needs no launcher; use it when Poly-QL is pip-installed in the Python your agent uses.

## Claude Code

### From the marketplace (this repo is the marketplace)

```text
/plugin marketplace add Mehul-Gupta-SMH/PolyQL
/plugin install polyql@polyql
```

or from a shell: `claude plugin marketplace add Mehul-Gupta-SMH/PolyQL && claude plugin install polyql@polyql`.

Then run `/plugin configure polyql@polyql` and set **Poly-QL checkout** to your clone (the marketplace install is a cached copy). From a shell you can pass it at install time: `--config polyql_home=/path/to/PolyQL`. Run `/reload-plugins`, then `/mcp` should show `plugin:polyql:polyql`.

### From a local clone

```text
/plugin marketplace add /path/to/PolyQL
/plugin install polyql@polyql
```

A marketplace added from a local path loads the plugin in place, so the launcher finds the clone by itself and your edits apply on the next session start. To try it without installing: `claude --plugin-dir /path/to/PolyQL/plugins/polyql`.

### Plugin options

Asked when the plugin is enabled; all are optional.

| Option | Becomes | Purpose |
|---|---|---|
| `polyql_home` | `POLYQL_HOME` | Path to the Poly-QL checkout. Needed for a marketplace (cached) install |
| `python` | `POLYQL_PYTHON` | Interpreter with Poly-QL's dependencies and `mcp` |
| `instance` | used in the commands | Default database instance (empty means `default`, all instances) |
| `enable_generate` | `POLYQL_MCP_ENABLE_GENERATE` | `1` exposes `generate_sql`, which calls Poly-QL's LLM pipeline and costs tokens |
| `db_url` | `POLYQL_MCP_DB` | Read-only connection URL; turns on `execute_sql`. Stored in the system credential store. For per-instance URLs export `POLYQL_MCP_DB_<INSTANCE>` in the shell that starts Claude Code, see [mcp.md](mcp.md#running-queries-execute_sql) |

Tool names look like `mcp__plugin_polyql_polyql__search_schema` in permission rules and hooks.

## Codex

Codex reads MCP servers from `~/.codex/config.toml` and, in trusted projects, from `.codex/config.toml`.

One command (use the absolute path to the launcher; set the env vars you need):

```bash
codex mcp add polyql --env POLYQL_HOME=/path/to/PolyQL -- /path/to/PolyQL/plugins/polyql/bin/polyql-mcp
codex mcp list
```

Or paste this into `config.toml`:

```toml
[mcp_servers.polyql]
command = "/path/to/PolyQL/plugins/polyql/bin/polyql-mcp"
args = []
startup_timeout_sec = 30   # first start imports Poly-QL's dependencies

[mcp_servers.polyql.env]
POLYQL_HOME = "/path/to/PolyQL"
# POLYQL_PYTHON = "/path/to/PolyQL/.venv/bin/python"
# POLYQL_MCP_ENABLE_GENERATE = "1"
# POLYQL_MCP_DB = "postgresql://readonly@host/db"
```

**Skill.** Codex discovers skills in `.agents/skills` (per repository) and `~/.agents/skills` (per user), and follows symlinks:

```bash
mkdir -p ~/.agents/skills
ln -s /path/to/PolyQL/plugins/polyql/skills/polyql-sql ~/.agents/skills/polyql-sql    # or cp -r
```

**Codex plugin.** `plugins/polyql/.codex-plugin/plugin.json` bundles the skill and the MCP server, and the repo is a Codex marketplace (`.agents/plugins/marketplace.json`):

```bash
codex plugin marketplace add Mehul-Gupta-SMH/PolyQL
codex plugin add polyql@polyql
```

Codex copies the plugin into its cache, so the launcher can't find the checkout on its own: export `POLYQL_HOME` (and optionally `POLYQL_PYTHON`, `POLYQL_MCP_DB`, `POLYQL_MCP_ENABLE_GENERATE`) in the shell you start Codex from — the plugin passes exactly those through (`env_vars`). The server is started as `sh -c "exec ./bin/polyql-mcp"` with the plugin root as `cwd`, because Codex resolves a relative `command` against the session's directory, not the plugin's. Use `codex mcp add` above instead if you'd rather not install the plugin.

These manifests were checked against the Codex source (`codex-rs/core-plugins/src/manifest.rs`, `marketplace.rs`, `codex-rs/codex-mcp/src/plugin_config.rs`, `codex-rs/config/src/mcp_types.rs`, `codex-rs/cli/src/plugin_cmd.rs`) rather than a live `codex` install.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/mcp` shows `polyql` failed; log says `cannot find the Poly-QL checkout` | The plugin runs from the cache. Set the **Poly-QL checkout** option (or `POLYQL_HOME`) |
| `No module named mcp` / `backend` | The Python lacks the dependencies. Set `POLYQL_PYTHON`, or `pip install ".[mcp]"` in the checkout's venv |
| Tools exist but say there is no metadata | Ingest tables first; the server uses the same stores as the web app |
| First `search_schema` is slow | The embedding model and ChromaDB load on first use |
| `execute_sql` or `generate_sql` missing | Both are opt-in: set `db_url` / `enable_generate` (or the env vars) |
| Changed an option and nothing happened | Run `/reload-plugins` (Claude Code) or restart Codex |
| `Permission denied` on the launcher | `chmod +x plugins/polyql/bin/polyql-mcp` (git keeps the mode, zip downloads may not) |
| Windows | The launcher is a POSIX shell script. Use WSL, or skip the launcher: `command = "python"`, `args = ["-m", "backend.mcp_server"]` with `cwd` at the checkout |

Validate the Claude Code files with `claude plugin validate .` (marketplace) and `claude plugin validate plugins/polyql`.

## Why this layout

The repository is the marketplace and the plugin lives in `plugins/polyql/`. If the repo root were the plugin, installing it would copy the whole app (frontend, databases, results) into the cache. A marketplace install copies only the plugin directory either way, so the server code cannot ride along; `POLYQL_HOME` points the launcher back at a real checkout. The skill lives only in `plugins/polyql/skills/polyql-sql/` (a symlink from elsewhere would be skipped on a local install), and the docs link to it.

## Sources

Claude Code:
- https://code.claude.com/docs/en/plugins-reference (redirects to the plugin manifest reference)
- https://code.claude.com/docs/en/plugins/components.md
- https://code.claude.com/docs/en/plugins/loading.md
- https://code.claude.com/docs/en/plugin-marketplaces
- https://code.claude.com/docs/en/plugins/host-marketplace.md

Codex (the OpenAI docs host could not be fetched while writing; these were read through search excerpts and third-party quotes of the docs, so check them):
- https://developers.openai.com/codex/plugins/build
- https://developers.openai.com/codex/mcp
- https://developers.openai.com/codex/skills
- https://github.com/openai/codex/issues/16500 (quotes the `source.path` rule)
