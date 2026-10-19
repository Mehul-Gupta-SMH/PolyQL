"""
backend/mcp_server.py — PolyQL as an MCP server (context mode)

Exposes PolyQL's schema knowledge as MCP tools so an agent harness (Claude
Code, Codex, Claude Agent SDK, OpenAI Agents SDK, LangGraph, ...) can write
SQL itself with the right context. No LLM is called by these tools: the host
agent is the LLM, so no API key is needed and nothing is paid twice.

Tools (all read-only):
  list_instances     — database instances PolyQL has metadata for
  list_tables        — every table in an instance with its description
  search_schema      — tables, columns, join paths, business definitions and
                       OKF knowledge notes relevant to a question
  get_table_schema   — full column metadata for one table
  find_join_path     — how to join a set of tables (keys, alternatives, cardinality)
  get_business_terms — glossary definitions/formulas relevant to a question
  validate_sql       — parse check, read-only check and unknown-table check

Prompt:
  write_sql          — the recommended tool workflow for answering a question

Run:
  python -m backend.mcp_server                       # stdio (Claude Code, Codex)
  python -m backend.mcp_server --transport streamable-http --port 8765

The streamable-http transport has no authentication; it binds to 127.0.0.1
by default. Do not expose it beyond localhost without putting auth in front.

Heavy dependencies (embedding model, reranker, ChromaDB) load lazily on the
first search_schema / get_business_terms call, so the server starts fast.
"""

import argparse
import logging
import os
import re
import sys
from typing import TypedDict

# Ensure project root is on the path so `main` and friends import when run as a module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.mcpserver import MCPServer  # noqa: E402

logger = logging.getLogger(__name__)

# Cap tool output so one wide schema can't flood the agent's context
_MAX_OUTPUT_CHARS = 30_000

_INSTRUCTIONS = """\
PolyQL holds schema metadata, join paths, business definitions and curated
knowledge notes for one or more databases ("instances"). Use it to write
correct SQL yourself; PolyQL does not run queries.

Recommended workflow for a data question:
1. list_instances (if you don't know the instance name).
2. search_schema(question) — read the tables, columns, join paths,
   Business Definitions and Knowledge Notes it returns.
3. get_table_schema / find_join_path for anything missing.
4. Write the SQL. Use business-term formulas as given; follow the
   knowledge notes where they apply.
5. validate_sql(sql) and fix anything it reports before answering.
"""

mcp = MCPServer(name="polyql", title="PolyQL", instructions=_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cap(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + "\n\n… (truncated — ask for fewer tables or one table at a time)"


def _metadata_db():
    from Utilities.base_utils import accessDB, get_config_val

    tmddb = get_config_val("retrieval_config", ["tableMDdb"], True)
    return accessDB(tmddb["info_type"], tmddb["dbName"]), tmddb


_NO_METADATA = "PolyQL has no table metadata yet — ingest tables first (UI: Ingest Table, or the benchmark ingest scripts)."


def _known_tables(instance: str) -> dict | None:
    """
    {table_name: description} for *instance* ('default' = all instances).

    Returns None when the metadata store can't be read (e.g. nothing ingested
    yet, so the tableDesc table doesn't exist).
    """
    try:
        db, tmddb = _metadata_db()
        inst_filter = {} if instance == "default" else {"instance_name": instance}
        rows = db.get_data(tmddb["tableDescName"], inst_filter, ["tableName", "Desc"], fetchtype="All") or []
    except Exception as exc:
        logger.warning("MCP: table metadata unavailable — %s", exc)
        return None
    return {row[0]: (row[1] or "") for row in rows if row and row[0]}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_instances() -> list[dict]:
    """
    List the database instances PolyQL has metadata for.

    Returns [{instance_name, db_type}]. Pass instance_name to the other tools;
    'default' searches across all instances.
    """
    try:
        db, tmddb = _metadata_db()
        rows = db.get_data(tmddb["tableDescName"], {}, ["instance_name", "db_type"], fetchtype="All") or []
    except Exception as exc:
        logger.warning("MCP: table metadata unavailable — %s", exc)
        rows = []
    seen: dict = {}
    for row in rows:
        seen.setdefault(row[0] or "default", row[1] or "generic")
    instances = [{"instance_name": k, "db_type": v} for k, v in sorted(seen.items())]
    return instances or [{"instance_name": "default", "db_type": "generic"}]


@mcp.tool()
def list_tables(instance: str = "default") -> str:
    """
    List every table in an instance with its one-line description.

    Use this to browse; prefer search_schema to find the tables for a question.
    """
    tables = _known_tables(instance)
    if tables is None:
        return _NO_METADATA
    if not tables:
        return f"(no tables found for instance {instance!r})"
    return _cap("\n".join(f"- **{name}**: {desc}" for name, desc in sorted(tables.items())))


@mcp.tool()
def search_schema(question: str, instance: str = "default") -> str:
    """
    Find the schema context needed to answer a natural-language data question.

    Returns markdown with: Business Definitions (canonical formulas), Knowledge
    Notes (curated OKF guidance), the relevant tables with their key and
    relevant columns ("direct" = matched the question, "intermediate" = needed
    to join), and the join paths between them. Write SQL only against these.
    """
    from APIManager.PromptBuilder import PromptBuilder
    from main import _get_business_context, getRelevantContext

    context = getRelevantContext(question, instance_name=instance)
    context["glossary_hits"] = _get_business_context(question, instance_name=instance)
    return _cap(PromptBuilder.format_schema(context))


@mcp.tool()
def get_table_schema(table: str, instance: str = "default") -> str:
    """
    Get the full column metadata for one table: name, type, constraints,
    description, and (for pipeline-derived tables) source expressions.
    """
    from main import _get_full_table_schema

    known = _known_tables(instance)
    if known is None:
        return _NO_METADATA
    match = next((t for t in known if t.lower() == table.lower()), None)
    if match is None:
        return f"Table {table!r} not found in instance {instance!r}. Use list_tables or search_schema."
    return _cap(_get_full_table_schema(match, instance_name=instance))


@mcp.tool()
def find_join_path(tables: list[str], instance: str = "default") -> str:
    """
    Explain how to join a set of tables, using PolyQL's join graph.

    Lists each hop with its join condition and cardinality (1:1, 1:n, n:1, n:m).
    When two tables can be joined more than one way (e.g. order_date vs
    ship_date → dim_date), every alternative is listed: pick the one the
    question needs. Tables that can't be connected are reported.
    """
    from MetadataManager.MetadataStore.relationdb import kuzuDB

    from backend.app import _determine_join_type

    if len(tables) < 2:
        return "Pass at least two tables."

    graph = kuzuDB.getObj(instance)
    names = [t.lower() for t in tables]
    unknown = [t for t in names if t not in graph]
    relations = kuzuDB.getRelations(graph, [t for t in names if t in graph])

    lines = []
    if relations:
        lines.append("## Join path")
        for rel in relations:
            keys = (rel.get("edge_attributes") or {}).get("JoinKeys", "") or "(no join keys recorded)"
            kind = _determine_join_type(keys, rel["source"], rel["target"])
            lines.append(f"- {rel['source']} → {rel['target']} [{kind}] ON {keys}")

    connected = {r["source"] for r in relations} | {r["target"] for r in relations}
    unreached = [t for t in names if t in graph and t not in connected]
    if unreached:
        lines.append(f"\nNo join path found to: {', '.join(unreached)}")
    if unknown:
        lines.append(f"\nUnknown tables (not in the join graph): {', '.join(unknown)}")
    return "\n".join(lines).strip() or "No join path found."


@mcp.tool()
def get_business_terms(question: str, instance: str = "default") -> str:
    """
    Get the business glossary terms relevant to a question: definition,
    canonical formula and table dependencies. Use the formulas as given.
    """
    from APIManager.PromptBuilder import PromptBuilder
    from main import _get_business_context

    hits = _get_business_context(question, instance_name=instance)
    if not hits:
        return "No matching business terms."
    return _cap("\n".join(PromptBuilder.format_business_definitions(hits)).rstrip())


_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+((?:[`\"\[]?[\w$]+[`\"\]]?\.){0,2}[`\"\[]?[\w$]+[`\"\]]?)",
    re.IGNORECASE,
)
_READ_ONLY_TYPES = {"SELECT"}
# Removed before looking for table references: comments, string literals, and
# functions whose arguments use FROM (EXTRACT(YEAR FROM d), SUBSTRING(s FROM 2), ...)
_STRIP_RES = [
    re.compile(r"--[^\n]*"),
    re.compile(r"/\*.*?\*/", re.DOTALL),
    re.compile(r"'(?:[^']|'')*'"),
    re.compile(r"\b(?:EXTRACT|SUBSTRING|TRIM|POSITION|OVERLAY)\s*\([^()]*\)", re.IGNORECASE),
]


def _table_refs(sql: str) -> tuple:
    """
    Table names after FROM / JOIN (heuristic; quotes removed, lowercased).

    Returns (refs, cleaned_sql) — the SQL with comments, literals and FROM-using
    functions removed, so CTE detection runs on the same text.
    """
    for pattern in _STRIP_RES:
        sql = pattern.sub(" ", sql)
    refs = []
    for ref in _TABLE_REF_RE.findall(sql):
        name = re.sub(r"[`\"\[\]]", "", ref).lower()
        if name not in refs:
            refs.append(name)
    return refs, sql


class SQLCheck(TypedDict):
    valid: bool
    read_only: bool
    statement_types: list[str]
    unknown_tables: list[str]
    errors: list[str]
    warnings: list[str]


@mcp.tool()
def validate_sql(sql: str, instance: str = "default") -> SQLCheck:
    """
    Check SQL before you return it.

    Reports whether it parses, whether every statement is read-only
    (SELECT / WITH … SELECT), and any FROM/JOIN table that PolyQL doesn't know
    (CTE names are ignored). Does not run the query or check columns.
    Returns {valid, read_only, statement_types, unknown_tables, errors, warnings}.
    """
    import sqlparse

    errors, warnings = [], []
    statements = [s for s in sqlparse.parse(sql or "") if str(s).strip()]
    if not statements:
        return {"valid": False, "read_only": False, "statement_types": [],
                "unknown_tables": [], "errors": ["No SQL statement found."], "warnings": []}

    types = [s.get_type() for s in statements]
    read_only = all(t in _READ_ONLY_TYPES for t in types)
    if "UNKNOWN" in types:
        errors.append("Could not recognise the statement type; check the syntax.")
    if not read_only:
        errors.append(f"Not read-only: {', '.join(t for t in types if t not in _READ_ONLY_TYPES)}.")

    refs, cleaned = _table_refs(sql)
    ctes = {
        m.lower()
        for m in re.findall(r"(?:\bWITH(?:\s+RECURSIVE)?|,)\s+([\w$]+)\s+AS\s*\(", cleaned, re.IGNORECASE)
    }
    known_tables = _known_tables(instance)
    unknown = []
    if known_tables is None:
        warnings.append("Table metadata unavailable — unknown-table check skipped.")
    else:
        known = {t.lower() for t in known_tables}
        known_short = {t.rsplit(".", 1)[-1] for t in known}
        unknown = [
            name for name in refs
            if name not in ctes and name not in known and name.rsplit(".", 1)[-1] not in known_short
        ]
    if unknown:
        errors.append(f"Unknown tables: {', '.join(unknown)}.")

    return {
        "valid": not errors,
        "read_only": read_only,
        "statement_types": types,
        "unknown_tables": unknown,
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

@mcp.prompt()
def write_sql(question: str, instance: str = "default", dialect: str = "ANSI SQL") -> str:
    """Answer a data question with SQL using PolyQL's schema context."""
    return (
        f"Write a {dialect} query that answers: {question}\n\n"
        f"Use the PolyQL tools for instance {instance!r}:\n"
        "1. Call search_schema with the question and read every section it returns.\n"
        "2. If a table or join is missing, call get_table_schema or find_join_path.\n"
        "3. Use Business Definitions formulas exactly; follow Knowledge Notes where they apply.\n"
        "4. Use only tables and columns PolyQL returned. Watch n:m joins for double counting.\n"
        "5. Call validate_sql and fix every error it reports.\n"
        "Return the final SQL and a one-line explanation of the tables and joins used."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run PolyQL as an MCP server (context mode).")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="streamable-http only (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="streamable-http only (default: 8765)")
    args = parser.parse_args(argv)

    # Logs go to stderr; in stdio mode stdout carries the protocol
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            logger.warning("Serving PolyQL MCP on %s without authentication", args.host)
        mcp.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
