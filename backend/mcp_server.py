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
  validate_sql       — parse, read-only, unknown-table and unknown-column checks
  get_lineage        — tables connected to a table in the graph
  get_derivatives    — pipeline sources / derived tables of a table

Prompt:
  write_sql          — the recommended tool workflow for answering a question

Opt-in tools:
  execute_sql        — run one read-only SELECT; needs POLYQL_MCP_DB[_<INSTANCE>]
  generate_sql       — PolyQL's own LLM pipeline; needs --enable-generate

Resources: polyql://tables/{instance}, polyql://schema/{instance}/{table},
polyql://glossary/{term}

Run:
  python -m backend.mcp_server                       # stdio (Claude Code, Codex)
  python -m backend.mcp_server --transport streamable-http --port 8765

streamable-http binds to 127.0.0.1 by default. Set POLYQL_MCP_TOKEN (and/or
JWT_SECRET to accept PolyQL login tokens) to require `Authorization: Bearer`;
on any other host the server refuses to start without auth unless
--insecure-no-auth is given.

Heavy dependencies (embedding model, reranker, ChromaDB) load lazily on the
first search_schema / get_business_terms call, so the server starts fast.
"""

import argparse
import logging
import os
import re
import sys
from collections import ChainMap
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
correct SQL yourself. If execute_sql is configured you can run read-only
SELECTs to check results.

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


def _known_columns(instance: str) -> dict | None:
    """
    {table_name_lower: {column_lower, ...}} for *instance*; None when the
    metadata store can't be read.
    """
    try:
        db, tmddb = _metadata_db()
        inst_filter = {} if instance == "default" else {"instance_name": instance}
        rows = db.get_data(tmddb["tableColName"], inst_filter, ["TableName", "ColumnName"], fetchtype="All") or []
    except Exception as exc:
        logger.warning("MCP: column metadata unavailable — %s", exc)
        return None
    columns: dict = {}
    for table, column in rows:
        if table and column:
            columns.setdefault(table.lower(), set()).add(column.lower())
    return columns


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

    Connects all the tables with one minimal set of joins, preferring key-to-key
    hops over many-to-many. Each hop shows its join condition and cardinality
    (1:1, 1:n, n:1, n:m); 1:n and n:m hops multiply rows, so aggregate before
    joining or use COUNT(DISTINCT …). When two tables can be joined more than
    one way (e.g. order_date vs ship_date → dim_date), every alternative is
    listed: pick the one the question needs. Pipeline lineage edges are not
    used. Tables that can't be connected are reported.
    """
    from MetadataManager.MetadataStore.relationdb import kuzuDB
    from MetadataManager.MetadataStore.relationdb.joinpath import connect_tables, load_unique_columns

    if len(tables) < 2:
        return "Pass at least two tables."

    graph = kuzuDB.getObj(instance)
    names = list(dict.fromkeys(t.lower() for t in tables))
    unknown = [t for t in names if t not in graph]
    relations, unreached = connect_tables(graph, [t for t in names if t in graph],
                                          load_unique_columns(instance))

    lines = []
    if relations:
        lines.append("## Join path")
        for rel in relations:
            lines.append(f"- {rel['source']} → {rel['target']} [{rel['cardinality']}] ON {rel['join_keys']}")
            for alt in rel["alternatives"][1:]:
                lines.append(f"    - or ON {alt}")
        fan_out = [f"{r['source']} → {r['target']}" for r in relations if r["cardinality"] in ("1:n", "n:m")]
        if fan_out:
            lines.append(f"\nRow multiplication: {', '.join(fan_out)}. Aggregate before joining or use COUNT(DISTINCT …).")
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


@mcp.tool()
def get_lineage(table: str, instance: str = "default") -> str:
    """
    Show the tables connected to a table in PolyQL's graph (its connected
    component), with the join keys between them. Use it to see where a table
    sits among its neighbours before choosing joins.
    """
    from fastapi import HTTPException

    from backend.app import get_lineage as _lineage

    try:
        data = _lineage(table, instance_name=instance)
    except HTTPException as exc:
        return str(exc.detail)
    lines = [f"## Lineage of {data['center']}"]
    related = [n["id"] for n in data["nodes"] if n["role"] != "center"]
    lines.append(f"Connected tables: {', '.join(related) if related else '(none)'}")
    if data["edges"]:
        lines.append("")
        for edge in data["edges"]:
            keys = edge.get("joinKeys") or "(no join keys recorded)"
            lines.append(f"- {edge['source']} — {edge['target']} [{edge['joinType']}] ON {keys}")
    return _cap("\n".join(lines))


@mcp.tool()
def get_derivatives(table: str, instance: str = "default") -> str:
    """
    Show pipeline lineage for a table: which derived tables are built from it
    and, if it is itself derived, which source tables it comes from. Prefer
    querying the source tables when a derived table's definition is unclear.
    """
    from backend.app import get_derivatives as _derivatives

    try:
        data = _derivatives(table, instance_name=instance)
    except Exception as exc:
        logger.warning("MCP: derivatives lookup failed for %r — %s", table, exc)
        return _NO_METADATA

    def _fmt(items):
        return "\n".join(f"- **{t['name']}**: {t['description']}" for t in items) or "(none)"

    kind = "derived (built by a pipeline)" if data["is_derived"] else "a source table"
    return _cap(
        f"## {data['table']} is {kind}\n\n"
        f"### Built from\n{_fmt(data['parent_tables'])}\n\n"
        f"### Tables derived from it\n{_fmt(data['derived_tables'])}"
    )


_IDENT = r"[`\"\[]?[\w$]+[`\"\]]?"
_TABLE_REF_RE = re.compile(
    rf"\b(?:FROM|JOIN)\s+((?:{_IDENT}\.){{0,2}}{_IDENT})(?:\s+(?:AS\s+)?({_IDENT}))?",
    re.IGNORECASE,
)
# Words that can follow a table reference but are not an alias
_NOT_ALIAS = {
    "on", "using", "where", "join", "inner", "left", "right", "full", "outer", "cross",
    "natural", "group", "order", "having", "limit", "offset", "union", "intersect",
    "except", "window", "qualify", "fetch", "for", "lateral", "tablesample", "with",
}
_COLUMN_REF_RE = re.compile(r"\b([\w$]+)\.([\w$]+|\*)(?!\s*\()")
_READ_ONLY_TYPES = {"SELECT"}
# Removed before looking for table references: comments, string literals, and
# functions whose arguments use FROM (EXTRACT(YEAR FROM d), SUBSTRING(s FROM 2), ...)
_STRIP_RES = [
    re.compile(r"--[^\n]*"),
    re.compile(r"/\*.*?\*/", re.DOTALL),
    re.compile(r"'(?:[^']|'')*'"),
    re.compile(r"\b(?:EXTRACT|SUBSTRING|TRIM|POSITION|OVERLAY)\s*\([^()]*\)", re.IGNORECASE),
]


def _unquote(name: str) -> str:
    return re.sub(r"[`\"\[\]]", "", name).lower()


def _table_refs(sql: str) -> tuple:
    """
    Parse FROM / JOIN clauses (heuristic).

    Returns (refs, aliases, cleaned_sql, column_sql):
      refs        table names in order of appearance (quotes removed, lowercased)
      aliases     {alias_or_short_name: table_name}
      cleaned_sql the SQL with comments, literals and FROM-using functions removed
      column_sql  cleaned_sql with the table references blanked out, so that
                  schema.table isn't mistaken for alias.column
    """
    for pattern in _STRIP_RES:
        sql = pattern.sub(" ", sql)
    refs, aliases = [], {}
    for match in _TABLE_REF_RE.finditer(sql):
        name = _unquote(match.group(1))
        if name not in refs:
            refs.append(name)
        aliases.setdefault(name.rsplit(".", 1)[-1], name)
        alias = _unquote(match.group(2) or "")
        if alias and alias not in _NOT_ALIAS:
            aliases[alias] = name
    column_sql = _TABLE_REF_RE.sub(" ", sql)
    column_sql = re.sub(r"[`\"\[\]]", "", column_sql)
    return refs, aliases, sql, column_sql


_INTO_RE = re.compile(r"\bINTO\b", re.IGNORECASE)


def _writes_via_into(sql: str) -> bool:
    """SELECT … INTO (PostgreSQL new table, MySQL OUTFILE/DUMPFILE) writes despite being a SELECT."""
    for pattern in _STRIP_RES:
        sql = pattern.sub(" ", sql)
    return bool(_INTO_RE.search(sql))


def _unqualified_columns(statement) -> list:
    """
    Bare column names in a parsed statement (sqlparse), lowercased.

    Skipped: qualified parts (x.col), function names (fn(...)), and aliases —
    a name right after AS, ')', END or another name/literal (`count(*) n`,
    `orders o`) is an alias, and later uses of it (ORDER BY n) are skipped too.
    """
    from sqlparse import tokens as T

    toks = [t for t in statement.flatten() if not t.is_whitespace and t.ttype not in T.Comment]
    aliases, candidates = set(), []
    for i, tok in enumerate(toks):
        if tok.ttype is not T.Name:
            continue
        prev = toks[i - 1] if i else None
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if nxt is not None and nxt.value in (".", "("):
            continue
        if prev is not None and prev.value == ".":
            continue
        name = tok.value.strip('`"[]').lower()
        if prev is not None and (
            prev.normalized in ("AS", "END") or prev.value == ")"
            or prev.ttype is T.Name or prev.ttype in T.Literal
        ):
            aliases.add(name)
            continue
        candidates.append(name)
    return [c for c in dict.fromkeys(candidates) if c not in aliases]

# --- Column check: one scope per SELECT block --------------------------------
# Subqueries are cut out of the SQL and replaced by "(__sqN__)" so that every
# SELECT block (CTE body, derived table, set-operation branch, EXISTS / IN /
# scalar subquery) can be judged against its own FROM/JOIN sources only.
_QUERY_START_RE = re.compile(
    r"\s*(?:SELECT|WITH)\b|\s*\(__sq\d+__\)\s*(?:UNION|INTERSECT|EXCEPT|MINUS)\b", re.IGNORECASE
)
_SET_OP_RE = re.compile(r"\b(?:UNION(?:\s+ALL)?|INTERSECT|EXCEPT|MINUS)\b", re.IGNORECASE)
_CTE_DEF_RE = re.compile(
    rf"\s*(?:WITH(?:\s+RECURSIVE)?\s+|,\s*)({_IDENT})\s*(?:\(([^()]*)\))?\s*AS\s*"
    r"(?:(?:NOT\s+)?MATERIALIZED\s*)?\(__sq(\d+)__\)",
    re.IGNORECASE,
)
# (LATERAL) subquery, with the alias and column list that follow it as a derived table
_SUBQUERY_RE = re.compile(
    rf"(LATERAL\s+)?\(__sq(\d+)__\)(?:\s*(?:AS\s+)?({_IDENT})(?:\s*\(([^()]*)\))?)?", re.IGNORECASE
)
_CLAUSE_RE = re.compile(r"\b(?:WHERE|GROUP|HAVING|ORDER|LIMIT|SELECT|WINDOW|QUALIFY)\b", re.IGNORECASE)
_SELECT_LIST_RE = re.compile(r"\s*SELECT\b(?:\s+(?:DISTINCT|ALL)\b)?(.*?)(?:\bFROM\b.*)?$", re.IGNORECASE | re.DOTALL)
_ITEM_ALIAS_RE = re.compile(rf"\bAS\s+({_IDENT})$", re.IGNORECASE)
_ITEM_COLUMN_RE = re.compile(rf"(?:{_IDENT}\.)*({_IDENT})$")


def _scan_text(sql: str) -> str:
    """SQL without comments; string literals emptied (kept as '' so sqlparse still sees a literal)."""
    for pattern in _STRIP_RES[:2]:
        sql = pattern.sub(" ", sql)
    return _STRIP_RES[2].sub("''", sql)


def _split_subqueries(sql: str):
    """
    Cut every parenthesised SELECT / WITH out of *sql*.

    Returns (top, subs) — *top* with each subquery replaced by "(__sqN__)", *subs[N]*
    the subquery's own text (which may itself hold placeholders of lower number) —
    or None when the parentheses don't balance.
    """
    subs, stack = [], [[]]
    for ch in sql:
        if ch == "(":
            stack.append([])
        elif ch == ")":
            if len(stack) == 1:
                return None
            body = "".join(stack.pop())
            if _QUERY_START_RE.match(body):
                subs.append(body)
                body = f"__sq{len(subs) - 1}__"
            stack[-1].append(f"({body})")
        else:
            stack[-1].append(ch)
    return ("".join(stack[0]), subs) if len(stack) == 1 else None


def _split_commas(text: str) -> list:
    """Split on commas outside parentheses."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    return parts + [text[start:]]


def _in_from_clause(prefix: str) -> bool:
    """True when a subquery written right after *prefix* is a FROM / JOIN / comma-joined source."""
    tail = re.split(r"\bFROM\b", prefix, flags=re.IGNORECASE)
    if len(tail) < 2 or _CLAUSE_RE.search(tail[-1]):
        return False
    return bool(re.search(r"(?:\bJOIN|,|^)\s*$", tail[-1], re.IGNORECASE))


def _select_items(block: str):
    """The select-list items of a SELECT block, or None when it isn't one."""
    match = _SELECT_LIST_RE.match(block)
    return [item.strip() for item in _split_commas(match.group(1))] if match else None


def _item_name(item: str):
    """Output column name of a select item (`expr AS name`, `t.col`, `col`), else None."""
    match = _ITEM_ALIAS_RE.search(item) or _ITEM_COLUMN_RE.match(item)
    name = _unquote(match.group(1)) if match else None
    return None if name is None or name.isdigit() or name in ("null", "true", "false") else name


def _output_columns(items, explicit):
    """
    Column names a CTE / derived table exposes, or None when they can't be told:
    a `*`, an expression without an alias, or an explicit column list whose length
    doesn't provably match.
    """
    if items is None or any(item.endswith("*") for item in items):
        return None
    if explicit is not None:
        return set(explicit) if len(explicit) == len(items) else None
    names = [_item_name(item) for item in items]
    return set(names) if None not in names else None


class _Block:
    """One SELECT block: its sources and the enclosing block it may be correlated with."""

    def __init__(self, outer=None):
        self.sources = {}  # alias -> set of column names, or None when unknown
        self.outer = outer

    def chain(self):
        block = self
        while block is not None:
            yield block
            block = block.outer

    def lookup(self, qualifier: str):
        """Columns of the source *qualifier* names here or in an enclosing block (None: unknown)."""
        for block in self.chain():
            if qualifier in block.sources:
                return block.sources[qualifier]
        return None


class _ColumnChecker:
    """
    Finds unknown columns scope by scope. Whenever the answer is uncertain
    (unknown table, `*` or unnamed expression in a CTE / derived table,
    unbalanced parentheses) the columns concerned are not reported.

      qualified `q.col`  checked against the source q in this block, else in an
                         enclosing block (correlated subquery)
      bare `col`         checked against this block's sources — plus the enclosing
                         blocks' for a correlated subquery — and only when every
                         one of them has known columns
    """

    def __init__(self, table_columns, bare: bool):
        self.table_columns = table_columns  # table name -> set of columns or None
        self.bare = bare
        self.subs = []
        self.unknown = []

    def run(self, sql: str) -> list:
        split = _split_subqueries(_scan_text(sql))
        if split is None:
            return []
        top, self.subs = split
        for statement in top.split(";"):
            if statement.strip():
                self._query(statement, None, ChainMap())
        return self.unknown

    def _query(self, text, outer, ctes):
        """A WITH header plus set-operation branches; returns the select items of the first branch."""
        ctes = ctes.new_child()
        defs, pos = [], 0
        while match := _CTE_DEF_RE.match(text, pos):
            name, explicit, sub = _unquote(match.group(1)), match.group(2), int(match.group(3))
            explicit = [_unquote(c.strip()) for c in explicit.split(",")] if explicit else None
            defs.append((name, explicit, sub))
            ctes[name] = None  # visible (columns unknown) to itself and later CTEs
            pos = match.end()
        for name, explicit, sub in defs:
            ctes[name] = _output_columns(self._query(self.subs[sub], None, ctes), explicit)
        first = None
        for i, branch in enumerate(_SET_OP_RE.split(text[pos:])):
            items = self._block(branch, outer, ctes)
            first = items if i == 0 else first
        return first

    def _source_columns(self, table, ctes):
        columns = ctes[table] if table in ctes else self.table_columns(table)
        return set(columns) if columns else None

    def _block(self, text, outer, ctes):
        refs, aliases, _, column_sql = _table_refs(text)
        block = _Block(outer)
        for alias, table in aliases.items():
            block.sources[alias] = self._source_columns(table, ctes)
        correlated = []
        for match in _SUBQUERY_RE.finditer(text):
            lateral, sub, alias, explicit = match.groups()
            if not _in_from_clause(text[:match.start()]):
                correlated.append(int(sub))
                continue
            items = self._query(self.subs[int(sub)], block if lateral else None, ctes)
            alias = _unquote(alias or "")
            if alias in _NOT_ALIAS:
                alias, explicit = "", None
            explicit = [_unquote(c.strip()) for c in explicit.split(",")] if explicit else None
            block.sources[alias or f"__sq{sub}__"] = _output_columns(items, explicit)

        for qualifier, column in _COLUMN_REF_RE.findall(column_sql):
            columns = block.lookup(qualifier.lower())
            if columns is not None and column != "*" and column.lower() not in columns:
                self._report(f"{qualifier.lower()}.{column.lower()}")
        self._check_bare(text, block)

        for sub in correlated:
            self._query(self.subs[sub], block, ctes)
        return _select_items(text)

    def _check_bare(self, text, block):
        import sqlparse

        chain = list(block.chain())
        if not (self.bare and block.sources and _SELECT_LIST_RE.match(text)
                and all(None not in b.sources.values() for b in chain)):
            return
        available = {c for b in chain for columns in b.sources.values() for c in columns}
        skip = {alias for b in chain for alias in b.sources}
        statements = [s for s in sqlparse.parse(text) if str(s).strip()]
        for column in _unqualified_columns(statements[0]) if statements else []:
            if column not in available and column not in skip and not column.startswith("__sq"):
                self._report(column)

    def _report(self, ref):
        if ref not in self.unknown:
            self.unknown.append(ref)


class SQLCheck(TypedDict):
    valid: bool
    read_only: bool
    statement_types: list[str]
    unknown_tables: list[str]
    unknown_columns: list[str]
    errors: list[str]
    warnings: list[str]


@mcp.tool()
def validate_sql(sql: str, instance: str = "default") -> SQLCheck:
    """
    Check SQL before you return it.

    Reports whether it parses, whether every statement is read-only
    (SELECT / WITH … SELECT), any FROM/JOIN table that PolyQL doesn't know
    (CTE names are ignored), and any column that doesn't exist. Each SELECT
    (main query, CTE body, subquery, UNION branch) is checked against its own
    FROM/JOIN sources: qualified columns (alias.column) against their table;
    bare columns only when every source is known. A CTE or derived table
    exposes its output names (explicit column list, aliases, plain columns);
    with `*` or an unaliased expression its columns are unknown and checks
    that depend on it are skipped, as are outer-scope references from
    correlated subqueries. Does not run the query.
    Returns {valid, read_only, statement_types, unknown_tables,
    unknown_columns, errors, warnings}.
    """
    import sqlparse

    errors, warnings = [], []
    statements = [s for s in sqlparse.parse(sql or "") if str(s).strip()]
    if not statements:
        return {"valid": False, "read_only": False, "statement_types": [],
                "unknown_tables": [], "unknown_columns": [], "errors": ["No SQL statement found."],
                "warnings": []}

    types = [s.get_type() for s in statements]
    into = _writes_via_into(sql)
    read_only = all(t in _READ_ONLY_TYPES for t in types) and not into
    if "UNKNOWN" in types:
        errors.append("Could not recognise the statement type; check the syntax.")
    if not all(t in _READ_ONLY_TYPES for t in types):
        errors.append(f"Not read-only: {', '.join(t for t in types if t not in _READ_ONLY_TYPES)}.")
    if into:
        errors.append("Not read-only: SELECT … INTO writes a table or file.")

    refs, _, cleaned, _ = _table_refs(sql)
    ctes = {
        m.lower()
        for m in re.findall(r"(?:\bWITH(?:\s+RECURSIVE)?|,)\s+([\w$]+)\s*(?:\([^()]*\))?\s+AS\s*\(", cleaned, re.IGNORECASE)
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

    unknown_columns = []
    known_columns = _known_columns(instance) if known_tables is not None else None
    if known_columns is None:
        if known_tables is not None:
            warnings.append("Column metadata unavailable — column check skipped.")
    else:
        by_short = {t.rsplit(".", 1)[-1]: t for t in known_columns}

        def table_columns(name):
            return known_columns.get(name) or known_columns.get(by_short.get(name.rsplit(".", 1)[-1], ""))

        unknown_columns = _ColumnChecker(table_columns, bare=len(statements) == 1).run(sql)
    if unknown_columns:
        errors.append(f"Unknown columns: {', '.join(unknown_columns)}.")

    return {
        "valid": not errors,
        "read_only": read_only,
        "statement_types": types,
        "unknown_tables": unknown,
        "unknown_columns": unknown_columns,
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Execution (opt-in: needs a server-side connection string)
# ---------------------------------------------------------------------------

_DB_ENV_PREFIX = "POLYQL_MCP_DB"
_DEFAULT_MAX_ROWS = 100


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _connection_string(instance: str) -> str | None:
    """
    Connection string for *instance* from the environment — never from tool
    arguments, so agents can't point the server at arbitrary databases:
      POLYQL_MCP_DB_<INSTANCE>  (instance upper-cased, non-alphanumerics → _)
      POLYQL_MCP_DB             fallback for 'default' / any instance
    """
    key = f"{_DB_ENV_PREFIX}_{re.sub(r'[^A-Za-z0-9]', '_', instance).upper()}"
    return os.environ.get(key) or os.environ.get(_DB_ENV_PREFIX) or None


def _redact(message: str, connection_string: str) -> str:
    """Remove the connection string and any URL password from an error message."""
    message = message.replace(connection_string, "<connection>")
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1***@", message)


class QueryResult(TypedDict):
    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool
    latency_ms: float
    enforced: list[str]
    error: str


@mcp.tool()
def execute_sql(sql: str, instance: str = "default", max_rows: int = _DEFAULT_MAX_ROWS,
                question: str = "") -> QueryResult:
    """
    Run ONE read-only SELECT against the instance's database and return rows.

    Only works when the server operator has configured a connection
    (POLYQL_MCP_DB_<INSTANCE> or POLYQL_MCP_DB). Rejected before execution:
    anything that isn't a single SELECT / WITH … SELECT. The transaction is
    always rolled back, rows are capped (max_rows, server limit
    POLYQL_MCP_MAX_ROWS, default 1000) and a statement timeout applies where
    the database supports it (POLYQL_MCP_TIMEOUT_S, default 30).
    Pass the user's question to have the outcome recorded with it.
    Returns {columns, rows, row_count, truncated, latency_ms, enforced, error};
    error is "" on success.
    """
    import time

    def fail(message: str) -> QueryResult:
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False,
                "latency_ms": 0.0, "enforced": [], "error": message}

    connection = _connection_string(instance)
    if not connection:
        return fail(
            "Execution is not configured for this server. The operator can enable it by "
            f"setting {_DB_ENV_PREFIX}_<INSTANCE> or {_DB_ENV_PREFIX} to a read-only connection string."
        )

    import sqlparse

    statements = [s for s in sqlparse.parse(sql or "") if str(s).strip()]
    if len(statements) != 1:
        return fail("Pass exactly one SQL statement.")
    if statements[0].get_type() not in _READ_ONLY_TYPES:
        return fail(f"Only SELECT queries can be executed (got {statements[0].get_type()}).")
    if _writes_via_into(sql):
        return fail("SELECT … INTO is not allowed: it writes a table or file.")

    from backend.executor import execute_read_only
    from validation.outcome_store import record as record_outcome

    limit = int(_env_number("POLYQL_MCP_MAX_ROWS", 1000))
    max_rows = max(1, min(int(max_rows), limit))
    timeout_s = _env_number("POLYQL_MCP_TIMEOUT_S", 30)
    context = {"generated_sql": sql, "query_type": "sql", "nl_query": question, "provider": "mcp"}

    t0 = time.monotonic()
    try:
        columns, rows, truncated, enforced = execute_read_only(sql, connection, max_rows, timeout_s)
    except Exception as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        message = _redact(f"{type(exc).__name__}: {exc}", connection)
        _safe_record(record_outcome, outcome="failure", latency_ms=latency_ms,
                     error_type=type(exc).__name__, error_msg=message, **context)
        result = fail(message)
        result["latency_ms"] = round(latency_ms, 1)
        return result

    latency_ms = (time.monotonic() - t0) * 1000
    _safe_record(record_outcome, outcome="success" if rows else "empty",
                 latency_ms=latency_ms, row_count=len(rows), **context)
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated,
            "latency_ms": round(latency_ms, 1), "enforced": enforced, "error": ""}


def _safe_record(record, **fields) -> None:
    """Outcome recording must never break a query result."""
    try:
        record(**fields)
    except Exception as exc:
        logger.warning("MCP: could not record query outcome — %s", exc)


# ---------------------------------------------------------------------------
# Generate mode (opt-in: --enable-generate)
# ---------------------------------------------------------------------------

class GeneratedQuery(TypedDict):
    type: str      # "sql" | "code" | "clarify"
    content: str   # the query/code, or the clarifying question
    error: str


def generate_sql(question: str, provider: str, instance: str = "default",
                 query_type: str = "sql", model: str = "") -> GeneratedQuery:
    """
    Generate the query with PolyQL's own LLM pipeline instead of writing it
    yourself. Calls the configured provider (costs tokens) and uses its API key
    from PolyQL's config. Prefer search_schema + writing SQL yourself; use this
    for clients without a capable model or to compare against PolyQL's output.

    provider: open_ai | anthropic | google | groq | codex | claude_code
    query_type: sql | spark_sql | dataframe_api | pandas
    Returns {type, content, error}: type "clarify" means PolyQL needs more
    detail — content is its question.
    """
    from main import generateQuery
    from backend.rate_limit import limiter, retry_after_header
    from backend.metrics import metrics

    allowed, retry = limiter.check("mcp")
    if not allowed:
        metrics.record_rate_limited("mcp:generate_sql")
        secs = retry_after_header(retry)
        return {"type": "error", "content": "",
                "error": f"Rate limit exceeded — try again in {secs} s."}

    if provider == "claude_code":
        logger.warning("MCP generate_sql: provider claude_code starts a nested Claude Code session")
    try:
        result = generateQuery(question, provider, query_type, model=model or None, instance_name=instance)
    except Exception as exc:
        return {"type": "error", "content": "", "error": f"{type(exc).__name__}: {exc}"}
    return {"type": result["type"], "content": result["content"], "error": ""}


_generate_enabled = False


def enable_generate() -> None:
    """Register generate_sql (generate mode). Off by default: it calls an LLM."""
    global _generate_enabled
    if not _generate_enabled:
        mcp.add_tool(generate_sql)
        _generate_enabled = True


# ---------------------------------------------------------------------------
# Resources (read-only views a client can attach as context)
# ---------------------------------------------------------------------------

@mcp.resource(
    "polyql://tables/{instance}",
    name="tables",
    description="Every table in an instance with its description ('default' = all instances).",
    mime_type="text/markdown",
)
def tables_resource(instance: str) -> str:
    return list_tables(instance)


@mcp.resource(
    "polyql://schema/{instance}/{table}",
    name="table-schema",
    description="Full column metadata for one table.",
    mime_type="text/markdown",
)
def schema_resource(instance: str, table: str) -> str:
    return get_table_schema(table, instance)


@mcp.resource(
    "polyql://glossary/{term}",
    name="glossary-term",
    description="One business glossary term: definition, canonical formula and table dependencies.",
    mime_type="text/markdown",
)
def glossary_resource(term: str) -> str:
    from APIManager.PromptBuilder import PromptBuilder
    from MetadataManager.GlossaryStore import get_term_by_name

    try:
        hit = get_term_by_name(term)
    except Exception as exc:
        logger.warning("MCP: glossary lookup failed for %r — %s", term, exc)
        hit = None
    if not hit:
        return f"No glossary term named {term!r}. Use get_business_terms to search."
    return "\n".join(PromptBuilder.format_business_definitions([hit])).rstrip()


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
        "5. Call validate_sql and fix every error it reports (qualify columns so they get checked).\n"
        "6. If execute_sql is configured, run the query and sanity-check the result.\n"
        "Return the final SQL and a one-line explanation of the tables and joins used."
    )


# ---------------------------------------------------------------------------
# HTTP authentication
# ---------------------------------------------------------------------------

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _token_checks() -> list:
    """
    Bearer-token checks enabled by the environment:
      POLYQL_MCP_TOKEN — a shared secret clients send as `Authorization: Bearer <token>`
      JWT_SECRET       — also accept PolyQL login JWTs (the web app must use the same secret)
    """
    import hmac

    checks = []
    static = os.environ.get("POLYQL_MCP_TOKEN")
    if static:
        checks.append(lambda token: hmac.compare_digest(token.encode(), static.encode()))
    if os.environ.get("JWT_SECRET"):
        def _jwt_ok(token: str) -> bool:
            from backend.auth import _decode_token
            try:
                _decode_token(token)
                return True
            except Exception:
                return False
        checks.append(_jwt_ok)
    return checks


class BearerAuth:
    """ASGI middleware: reject HTTP requests without a valid bearer token."""

    def __init__(self, app, checks: list):
        self.app = app
        self.checks = checks

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        scheme, _, token = header.partition(" ")
        if scheme.lower() == "bearer" and token and any(check(token.strip()) for check in self.checks):
            return await self.app(scope, receive, send)
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="polyql-mcp"')],
        })
        await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})


def http_app(host: str, checks: list):
    """The streamable-http ASGI app, wrapped in BearerAuth when checks are configured."""
    app = mcp.streamable_http_app(host=host)
    return BearerAuth(app, checks) if checks else app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run PolyQL as an MCP server (context mode).")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="streamable-http only (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="streamable-http only (default: 8765)")
    parser.add_argument("--enable-generate", action="store_true",
                        help="also expose generate_sql, which calls PolyQL's own LLM pipeline (costs tokens)")
    parser.add_argument("--insecure-no-auth", action="store_true",
                        help="allow streamable-http on a non-local host without authentication")
    args = parser.parse_args(argv)

    # Logs go to stderr; in stdio mode stdout carries the protocol
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    if args.enable_generate or os.environ.get("POLYQL_MCP_ENABLE_GENERATE") == "1":
        enable_generate()

    if args.transport == "stdio":
        mcp.run("stdio")
        return

    checks = _token_checks()
    if not checks:
        if args.host not in _LOCAL_HOSTS and not args.insecure_no_auth:
            parser.error(
                f"refusing to serve on {args.host} without authentication: set POLYQL_MCP_TOKEN "
                "(and/or JWT_SECRET), or pass --insecure-no-auth"
            )
        logger.warning("PolyQL MCP over HTTP has no authentication (set POLYQL_MCP_TOKEN to require a token)")

    import uvicorn

    uvicorn.run(http_app(args.host, checks), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
