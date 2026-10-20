"""
Tests for backend/mcp_server.py — PolyQL as an MCP server (context mode).

Every tool is called through a real MCP client connected in-process, so the
tool schemas, argument validation and result encoding are exercised too.
PolyQL internals (retrieval, glossary, join graph, metadata DB) are mocked.
"""

from unittest.mock import MagicMock, patch

import anyio
import pytest

from mcp import Client

import backend.mcp_server as srv

KNOWN = {"orders": "One row per order", "sales.customers": "Customer dimension"}
KNOWN_COLUMNS = {"orders": {"id", "customer_id", "placed_at", "total_usd"},
                 "sales.customers": {"id", "name", "note", "region"}}


def _run(coro_fn):
    return anyio.run(coro_fn)


def call(tool, args=None):
    async def go():
        async with Client(srv.mcp, raise_exceptions=True) as client:
            return await client.call_tool(tool, args or {})
    return _run(go)


def text(result):
    assert not result.is_error, result.content
    return result.content[0].text


@pytest.fixture()
def known_tables():
    with patch.object(srv, "_known_tables", return_value=dict(KNOWN)) as known, \
         patch.object(srv, "_known_columns", return_value={k: set(v) for k, v in KNOWN_COLUMNS.items()}):
        yield known


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_lists_all_tools_and_prompt(self):
        async def go():
            async with Client(srv.mcp) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                prompts = {p.name for p in (await client.list_prompts()).prompts}
                return tools, prompts, client.instructions
        tools, prompts, instructions = _run(go)

        assert tools == {"list_instances", "list_tables", "search_schema", "get_table_schema",
                         "find_join_path", "get_business_terms", "validate_sql", "get_lineage",
                         "get_derivatives", "execute_sql"}  # generate_sql is opt-in
        assert prompts == {"write_sql"}
        assert "search_schema" in instructions

    def test_validate_sql_publishes_output_schema(self):
        async def go():
            async with Client(srv.mcp) as client:
                return {t.name: t for t in (await client.list_tools()).tools}["validate_sql"]
        tool = _run(go)
        assert set(tool.output_schema["properties"]) == {
            "valid", "read_only", "statement_types", "unknown_tables", "unknown_columns", "errors", "warnings"}

    def test_write_sql_prompt_mentions_workflow(self):
        async def go():
            async with Client(srv.mcp) as client:
                return await client.get_prompt("write_sql", {"question": "monthly revenue", "instance": "prod"})
        prompt = _run(go)
        body = prompt.messages[0].content.text
        assert "monthly revenue" in body and "'prod'" in body
        assert body.index("search_schema") < body.index("validate_sql")


# ---------------------------------------------------------------------------
# Metadata listing
# ---------------------------------------------------------------------------

class TestListing:
    def _db(self, rows):
        db = MagicMock()
        db.get_data.return_value = rows
        return patch.object(srv, "_metadata_db", return_value=(db, {"tableDescName": "tableDesc"}))

    def test_list_instances_dedupes_and_sorts(self):
        with self._db([("prod", "snowflake"), (None, None), ("prod", "snowflake"), ("dev", "duckdb")]):
            result = call("list_instances")
        assert result.structured_content["result"] == [
            {"instance_name": "default", "db_type": "generic"},
            {"instance_name": "dev", "db_type": "duckdb"},
            {"instance_name": "prod", "db_type": "snowflake"},
        ]

    def test_list_instances_without_metadata_falls_back(self):
        with patch.object(srv, "_metadata_db", side_effect=RuntimeError("no such table: tableDesc")):
            result = call("list_instances")
        assert result.structured_content["result"] == [{"instance_name": "default", "db_type": "generic"}]

    def test_list_tables(self, known_tables):
        out = text(call("list_tables", {"instance": "prod"}))
        assert "- **orders**: One row per order" in out
        known_tables.assert_called_with("prod")

    def test_list_tables_without_metadata(self):
        with patch.object(srv, "_known_tables", return_value=None):
            assert "no table metadata yet" in text(call("list_tables"))

    def test_known_tables_scopes_instance_and_handles_missing_store(self):
        db = MagicMock()
        db.get_data.return_value = [("orders", None)]
        with patch.object(srv, "_metadata_db", return_value=(db, {"tableDescName": "tableDesc"})):
            assert srv._known_tables("prod") == {"orders": ""}
        assert db.get_data.call_args[0][1] == {"instance_name": "prod"}
        with patch.object(srv, "_metadata_db", side_effect=RuntimeError("boom")):
            assert srv._known_tables("prod") is None


# ---------------------------------------------------------------------------
# Schema context
# ---------------------------------------------------------------------------

class TestSchemaContext:
    def test_search_schema_renders_full_context(self):
        context = {
            "user_query": "monthly revenue",
            "table_list": {"direct": {"orders": {"description": "Order facts", "columns": []}},
                           "intermediate": {}},
            "join_keys": [],
            "okf_hits": [{"title": "Revenue", "type": "Metric", "tables": ["orders"], "body": "Net of returns."}],
        }
        glossary = [{"term_name": "AUM", "definition": "Assets under management.", "formula": "SUM(x)"}]
        with patch("main.getRelevantContext", return_value=context) as get_ctx, \
             patch("main._get_business_context", return_value=glossary):
            out = text(call("search_schema", {"question": "monthly revenue", "instance": "prod"}))

        get_ctx.assert_called_once_with("monthly revenue", instance_name="prod")
        assert out.index("## Business Definitions") < out.index("## Knowledge Notes") < out.index("### orders")
        assert "Net of returns." in out and "`SUM(x)`" in out

    def test_long_output_is_capped(self):
        with patch.object(srv, "_MAX_OUTPUT_CHARS", 50), \
             patch("main.getRelevantContext", return_value={
                 "user_query": "q" * 200, "table_list": {"direct": {}, "intermediate": {}}, "join_keys": []}), \
             patch("main._get_business_context", return_value=[]):
            out = text(call("search_schema", {"question": "q"}))
        assert out.endswith("one table at a time)") and len(out) < 200

    def test_get_table_schema_matches_case_insensitively(self, known_tables):
        with patch("main._get_full_table_schema", return_value="### orders\n| id |") as full:
            out = text(call("get_table_schema", {"table": "ORDERS"}))
        assert out.startswith("### orders")
        full.assert_called_once_with("orders", instance_name="default")

    def test_get_table_schema_unknown_table(self, known_tables):
        with patch("main._get_full_table_schema") as full:
            out = text(call("get_table_schema", {"table": "nope"}))
        assert "not found" in out
        full.assert_not_called()

    def test_get_business_terms(self):
        hits = [{"term_name": "AUM", "full_name": "Assets Under Management", "formula": "SUM(aum)"}]
        with patch("main._get_business_context", return_value=hits):
            out = text(call("get_business_terms", {"question": "aum"}))
        assert out.startswith("## Business Definitions")
        assert "### AUM (Assets Under Management)" in out and "## Database Schema" not in out

    def test_get_business_terms_none(self):
        with patch("main._get_business_context", return_value=[]):
            assert text(call("get_business_terms", {"question": "x"})) == "No matching business terms."


# ---------------------------------------------------------------------------
# Join paths
# ---------------------------------------------------------------------------

class TestFindJoinPath:
    def _graph(self, nodes):
        graph = MagicMock()
        graph.__contains__.side_effect = lambda n: n in nodes
        return graph

    def test_lists_hops_with_cardinality_and_alternatives(self):
        relations = [
            {"source": "orders", "target": "dim_date", "edge_attributes": {"JoinKeys": "orders.order_date_id = dim_date.id"}},
            {"source": "orders", "target": "dim_date", "edge_attributes": {"JoinKeys": "orders.ship_date_id = dim_date.id"}},
        ]
        with patch("MetadataManager.MetadataStore.relationdb.kuzuDB.getObj",
                   return_value=self._graph({"orders", "dim_date"})), \
             patch("MetadataManager.MetadataStore.relationdb.kuzuDB.getRelations", return_value=relations), \
             patch("backend.app._determine_join_type", return_value="n:1"):
            out = text(call("find_join_path", {"tables": ["Orders", "dim_date"]}))

        assert "- orders → dim_date [n:1] ON orders.order_date_id = dim_date.id" in out
        assert "orders.ship_date_id" in out

    def test_reports_unknown_and_unreachable_tables(self):
        with patch("MetadataManager.MetadataStore.relationdb.kuzuDB.getObj",
                   return_value=self._graph({"orders", "audit_log"})), \
             patch("MetadataManager.MetadataStore.relationdb.kuzuDB.getRelations", return_value=[]):
            out = text(call("find_join_path", {"tables": ["orders", "audit_log", "ghost"]}))

        assert "No join path found to: orders, audit_log" in out
        assert "Unknown tables (not in the join graph): ghost" in out

    def test_needs_two_tables(self):
        assert text(call("find_join_path", {"tables": ["orders"]})) == "Pass at least two tables."


# ---------------------------------------------------------------------------
# SQL validation
# ---------------------------------------------------------------------------

class TestValidateSQL:
    def check(self, sql):
        return call("validate_sql", {"sql": sql}).structured_content

    def test_valid_select_with_cte_qualified_names_and_extract(self, known_tables):
        result = self.check(
            "WITH recent AS (SELECT * FROM orders) "
            "SELECT EXTRACT(YEAR FROM r.placed_at), c.name FROM recent r "
            "JOIN \"Sales\".\"Customers\" c ON c.id = r.customer_id -- FROM ghost\n"
            "WHERE c.note <> 'from nowhere'"
        )
        assert result == {"valid": True, "read_only": True, "statement_types": ["SELECT"],
                          "unknown_tables": [], "unknown_columns": [], "errors": [], "warnings": []}

    def test_flags_unknown_tables(self, known_tables):
        result = self.check("SELECT * FROM orderz o JOIN customers c ON 1=1")
        assert not result["valid"]
        assert result["unknown_tables"] == ["orderz"]  # customers matches sales.customers

    @pytest.mark.parametrize("sql,kind", [
        ("DELETE FROM orders", "DELETE"),
        ("SELECT 1; DROP TABLE orders", "DROP"),
        ("INSERT INTO orders VALUES (1)", "INSERT"),
    ])
    def test_flags_writes(self, known_tables, sql, kind):
        result = self.check(sql)
        assert not result["valid"] and not result["read_only"]
        assert kind in result["statement_types"]

    def test_select_into_is_not_read_only(self, known_tables):
        result = self.check("SELECT * INTO backup FROM orders")
        assert not result["valid"] and not result["read_only"]
        assert "Not read-only: SELECT … INTO writes a table or file." in result["errors"]
        # INTO inside a string or comment is fine
        assert self.check("SELECT o.id FROM orders o WHERE o.id <> 'into' -- into\n")["read_only"]

    def test_empty_and_garbage(self, known_tables):
        assert self.check("   ")["errors"] == ["No SQL statement found."]
        assert not self.check("selec x frm y")["valid"]

    def test_flags_unknown_qualified_columns(self, known_tables):
        result = self.check(
            "SELECT o.totl, c.nme, o.id, c.* FROM orders o "
            "LEFT JOIN sales.customers AS c ON c.id = o.customer_id WHERE o.placed_at > '2024-01-01'"
        )
        assert result["unknown_columns"] == ["o.totl", "c.nme"]
        assert "Unknown columns: o.totl, c.nme." in result["errors"]

    def test_column_check_handles_table_names_quotes_and_functions(self, known_tables):
        result = self.check('SELECT orders.id, "o"."bogus", count(o.id) FROM "orders" "o" JOIN orders ON 1=1')
        assert result["unknown_columns"] == ["o.bogus"]

    def test_column_check_skips_ctes_subqueries_and_unknown_metadata(self, known_tables):
        assert self.check("WITH r AS (SELECT * FROM orders) SELECT r.anything FROM r")["unknown_columns"] == []
        assert self.check("SELECT x.foo FROM (SELECT id AS foo FROM orders) x")["unknown_columns"] == []
        with patch.object(srv, "_known_columns", return_value={}):
            assert self.check("SELECT o.whatever FROM orders o")["unknown_columns"] == []

    def test_column_metadata_unavailable_warns(self):
        with patch.object(srv, "_known_tables", return_value=dict(KNOWN)), \
             patch.object(srv, "_known_columns", return_value=None):
            result = self.check("SELECT o.id FROM orders o")
        assert result["valid"]
        assert result["warnings"] == ["Column metadata unavailable — column check skipped."]

    def test_without_metadata_skips_table_check(self):
        with patch.object(srv, "_known_tables", return_value=None):
            result = self.check("SELECT * FROM anything")
        assert result["valid"] and result["unknown_tables"] == []
        assert result["warnings"] == ["Table metadata unavailable — unknown-table check skipped."]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class TestMain:
    def test_stdio_is_default(self):
        with patch.object(srv.mcp, "run") as run:
            srv.main([])
        run.assert_called_once_with("stdio")

    def test_streamable_http_on_localhost_without_auth_warns(self, monkeypatch, caplog):
        monkeypatch.delenv("POLYQL_MCP_TOKEN", raising=False)
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with patch("uvicorn.run") as run, patch.object(srv, "http_app", return_value="app") as app:
            srv.main(["--transport", "streamable-http", "--port", "9000"])
        app.assert_called_once_with("127.0.0.1", [])
        run.assert_called_once_with("app", host="127.0.0.1", port=9000, log_level="info")
        assert "no authentication" in caplog.text

    def test_refuses_public_host_without_auth(self, monkeypatch):
        monkeypatch.delenv("POLYQL_MCP_TOKEN", raising=False)
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with patch("uvicorn.run") as run, pytest.raises(SystemExit):
            srv.main(["--transport", "streamable-http", "--host", "0.0.0.0"])
        run.assert_not_called()

    def test_public_host_allowed_with_token_or_explicit_override(self, monkeypatch):
        monkeypatch.setenv("POLYQL_MCP_TOKEN", "s3cret")
        with patch("uvicorn.run") as run, patch.object(srv, "http_app", return_value="app"):
            srv.main(["--transport", "streamable-http", "--host", "0.0.0.0"])
        run.assert_called_once()
        monkeypatch.delenv("POLYQL_MCP_TOKEN")
        with patch("uvicorn.run") as run, patch.object(srv, "http_app", return_value="app"):
            srv.main(["--transport", "streamable-http", "--host", "0.0.0.0", "--insecure-no-auth"])
        run.assert_called_once()

    def test_enable_generate_flag(self):
        with patch.object(srv, "enable_generate") as enable, patch.object(srv.mcp, "run"):
            srv.main(["--enable-generate"])
        enable.assert_called_once()


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------

class TestLineage:
    def test_get_lineage_formats_component(self):
        data = {"center": "orders", "nodes": [{"id": "orders", "role": "center"},
                                              {"id": "customers", "role": "related"}],
                "edges": [{"source": "orders", "target": "customers", "joinType": "n:1",
                           "joinKeys": "orders.customer_id = customers.id"}]}
        with patch("backend.app.get_lineage", return_value=data) as lineage:
            out = text(call("get_lineage", {"table": "orders", "instance": "prod"}))
        lineage.assert_called_once_with("orders", instance_name="prod")
        assert "Connected tables: customers" in out
        assert "- orders — customers [n:1] ON orders.customer_id = customers.id" in out

    def test_get_lineage_unknown_table(self):
        from fastapi import HTTPException

        with patch("backend.app.get_lineage", side_effect=HTTPException(404, "Table 'x' not found in schema.")):
            assert text(call("get_lineage", {"table": "x"})) == "Table 'x' not found in schema."

    def test_get_derivatives(self):
        data = {"table": "daily_sales", "is_derived": True,
                "parent_tables": [{"name": "orders", "description": "Order facts"}], "derived_tables": []}
        with patch("backend.app.get_derivatives", return_value=data):
            out = text(call("get_derivatives", {"table": "daily_sales"}))
        assert "daily_sales is derived" in out
        assert "- **orders**: Order facts" in out and "(none)" in out


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

class TestResources:
    def read(self, uri):
        async def go():
            async with Client(srv.mcp, raise_exceptions=True) as client:
                return (await client.read_resource(uri)).contents[0].text
        return _run(go)

    def test_templates_listed(self):
        async def go():
            async with Client(srv.mcp) as client:
                return {t.uri_template for t in (await client.list_resource_templates()).resource_templates}
        assert _run(go) == {"polyql://tables/{instance}", "polyql://schema/{instance}/{table}",
                            "polyql://glossary/{term}"}

    def test_schema_and_tables(self, known_tables):
        with patch("main._get_full_table_schema", return_value="### orders\n| id |") as full:
            assert self.read("polyql://schema/prod/orders").startswith("### orders")
        full.assert_called_once_with("orders", instance_name="prod")
        assert "- **orders**: One row per order" in self.read("polyql://tables/default")

    def test_glossary_term(self):
        term = {"term_name": "AUM", "definition": "Assets under management.", "formula": "SUM(aum)"}
        with patch("MetadataManager.GlossaryStore.get_term_by_name", return_value=term):
            out = self.read("polyql://glossary/AUM")
        assert "### AUM" in out and "`SUM(aum)`" in out
        with patch("MetadataManager.GlossaryStore.get_term_by_name", return_value=None):
            assert "No glossary term named 'nope'" in self.read("polyql://glossary/nope")


# ---------------------------------------------------------------------------
# execute_sql
# ---------------------------------------------------------------------------

class TestExecuteSQL:
    def run(self, args):
        return call("execute_sql", args).structured_content

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("POLYQL_MCP_DB", raising=False)
        monkeypatch.delenv("POLYQL_MCP_DB_DEFAULT", raising=False)
        result = self.run({"sql": "SELECT 1"})
        assert "not configured" in result["error"] and result["rows"] == []

    def test_connection_string_resolution(self, monkeypatch):
        monkeypatch.setenv("POLYQL_MCP_DB", "sqlite:///fallback.db")
        monkeypatch.setenv("POLYQL_MCP_DB_SALES_EU", "sqlite:///eu.db")
        assert srv._connection_string("sales-eu") == "sqlite:///eu.db"
        assert srv._connection_string("other") == "sqlite:///fallback.db"

    @pytest.mark.parametrize("sql,message", [
        ("DELETE FROM orders", "Only SELECT"),
        ("WITH x AS (SELECT 1) DELETE FROM orders", "Only SELECT"),
        ("SELECT 1; SELECT 2", "exactly one"),
        ("SELECT * INTO backup FROM orders", "INTO is not allowed"),
        ("SELECT * FROM orders INTO OUTFILE '/tmp/x'", "INTO is not allowed"),
        ("   ", "exactly one"),
    ])
    def test_rejects_before_connecting(self, monkeypatch, sql, message):
        monkeypatch.setenv("POLYQL_MCP_DB", "sqlite:///x.db")
        with patch("backend.executor.execute_read_only") as execute:
            result = self.run({"sql": sql})
        assert message in result["error"]
        execute.assert_not_called()

    def test_success_caps_rows_and_records_outcome(self, monkeypatch):
        monkeypatch.setenv("POLYQL_MCP_DB", "sqlite:///x.db")
        monkeypatch.setenv("POLYQL_MCP_MAX_ROWS", "50")
        monkeypatch.setenv("POLYQL_MCP_TIMEOUT_S", "5")
        with patch("backend.executor.execute_read_only",
                   return_value=(["id"], [[1], [2]], True, ["rollback"])) as execute, \
             patch("validation.outcome_store.record") as record:
            result = self.run({"sql": "SELECT id FROM orders", "max_rows": 500, "question": "ids?"})

        execute.assert_called_once_with("SELECT id FROM orders", "sqlite:///x.db", 50, 5.0)
        assert result["rows"] == [[1], [2]] and result["truncated"] and result["error"] == ""
        assert record.call_args.kwargs["outcome"] == "success"
        assert record.call_args.kwargs["provider"] == "mcp"
        assert record.call_args.kwargs["nl_query"] == "ids?"

    def test_failure_is_redacted_and_recorded(self, monkeypatch):
        dsn = "postgresql://alice:s3cret@db.internal:5432/sales"
        monkeypatch.setenv("POLYQL_MCP_DB", dsn)
        boom = RuntimeError(f"could not connect to {dsn} (postgresql://bob:hunter2@other/x)")
        with patch("backend.executor.execute_read_only", side_effect=boom), \
             patch("validation.outcome_store.record") as record:
            result = self.run({"sql": "SELECT 1"})

        assert "s3cret" not in result["error"] and "hunter2" not in result["error"]
        assert "<connection>" in result["error"] and "bob:***@" in result["error"]
        assert record.call_args.kwargs["outcome"] == "failure"
        assert "s3cret" not in record.call_args.kwargs["error_msg"]

    def test_recording_failure_does_not_break_result(self, monkeypatch):
        monkeypatch.setenv("POLYQL_MCP_DB", "sqlite:///x.db")
        with patch("backend.executor.execute_read_only", return_value=(["n"], [], False, [])), \
             patch("validation.outcome_store.record", side_effect=OSError("disk full")):
            result = self.run({"sql": "SELECT 1"})
        assert result["error"] == "" and result["row_count"] == 0


def _sqlalchemy_installed() -> bool:
    # Package metadata, not import: conftest puts a MagicMock in sys.modules
    from importlib.metadata import PackageNotFoundError, version
    try:
        version("sqlalchemy")
        return True
    except PackageNotFoundError:
        return False


_HAS_SQLALCHEMY = _sqlalchemy_installed()

# conftest.py mocks sqlalchemy in-process, so the real-database checks run in a
# subprocess that imports the real library.
_SQLITE_CHECKS = r"""
import sqlite3, sys
sys.path.insert(0, sys.argv[1])
from backend.executor import execute_read_only

path = sys.argv[2]
conn = sqlite3.connect(path)
conn.execute("CREATE TABLE orders (id INTEGER, total REAL)")
conn.executemany("INSERT INTO orders VALUES (?, ?)", [(i, i * 1.5) for i in range(20)])
conn.commit(); conn.close()
dsn = f"sqlite:///{path}"

columns, rows, truncated, enforced = execute_read_only("SELECT id, total FROM orders ORDER BY id", dsn, 3)
assert columns == ["id", "total"] and rows == [[0, 0.0], [1, 1.5], [2, 3.0]] and truncated, rows
assert {"rollback", "query_only", "statement_timeout"} <= set(enforced), enforced

try:
    execute_read_only("INSERT INTO orders VALUES (99, 1)", dsn)
    raise SystemExit("write was not blocked")
except Exception as exc:
    assert "readonly" in str(exc), exc

runaway = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
try:
    execute_read_only(runaway, dsn, timeout_s=0.5)
    raise SystemExit("timeout did not fire")
except Exception as exc:
    assert "interrupted" in str(exc), exc

assert sqlite3.connect(path).execute("SELECT count(*) FROM orders").fetchone() == (20,)
print("ok")
"""


@pytest.mark.skipif(not _HAS_SQLALCHEMY, reason="sqlalchemy not installed")
def test_execute_read_only_against_real_sqlite(tmp_path):
    """Rows + truncation, PRAGMA query_only blocks writes, timeout interrupts, data untouched."""
    import pathlib
    import subprocess
    import sys

    root = str(pathlib.Path(__file__).resolve().parents[1])
    proc = subprocess.run(
        [sys.executable, "-c", _SQLITE_CHECKS, root, str(tmp_path / "demo.db")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr


# ---------------------------------------------------------------------------
# Generate mode
# ---------------------------------------------------------------------------

class TestGenerateMode:
    def test_off_by_default_and_enabled_once(self, monkeypatch):
        async def names():
            async with Client(srv.mcp) as client:
                return {t.name for t in (await client.list_tools()).tools}

        monkeypatch.setattr(srv, "_generate_enabled", False)
        assert "generate_sql" not in _run(names)
        srv.enable_generate()
        srv.enable_generate()  # idempotent
        try:
            assert "generate_sql" in _run(names)
            with patch("main.generateQuery", return_value={"type": "sql", "content": "SELECT 1"}) as gen:
                result = call("generate_sql", {"question": "q", "provider": "anthropic", "instance": "prod"})
            gen.assert_called_once_with("q", "anthropic", "sql", model=None, instance_name="prod")
            assert result.structured_content == {"type": "sql", "content": "SELECT 1", "error": ""}
            with patch("main.generateQuery", side_effect=ValueError("bad provider")):
                result = call("generate_sql", {"question": "q", "provider": "nope"})
            assert result.structured_content["error"] == "ValueError: bad provider"
        finally:
            srv.mcp.remove_tool("generate_sql")


# ---------------------------------------------------------------------------
# HTTP auth
# ---------------------------------------------------------------------------

class TestBearerAuth:
    def asgi(self, checks, headers):
        sent, reached = [], []

        async def app(scope, receive, send):
            reached.append(True)

        async def send(message):
            sent.append(message)

        async def receive():
            return {}

        scope = {"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
        _run(lambda: srv.BearerAuth(app, checks)(scope, receive, send))
        status = sent[0]["status"] if sent else 200
        return status, bool(reached)

    def test_accepts_only_valid_bearer(self, monkeypatch):
        monkeypatch.setenv("POLYQL_MCP_TOKEN", "s3cret")
        monkeypatch.delenv("JWT_SECRET", raising=False)
        checks = srv._token_checks()
        assert self.asgi(checks, {"authorization": "Bearer s3cret"}) == (200, True)
        assert self.asgi(checks, {"authorization": "Bearer wrong"}) == (401, False)
        assert self.asgi(checks, {"authorization": "Basic s3cret"}) == (401, False)
        assert self.asgi(checks, {}) == (401, False)

    def test_accepts_polyql_jwt_when_secret_shared(self, monkeypatch):
        monkeypatch.delenv("POLYQL_MCP_TOKEN", raising=False)
        monkeypatch.setenv("JWT_SECRET", "shared")
        with patch("backend.auth._decode_token", side_effect=lambda t: {"sub": "u"} if t == "good.jwt" else 1 / 0):
            checks = srv._token_checks()
            assert self.asgi(checks, {"authorization": "Bearer good.jwt"}) == (200, True)
            assert self.asgi(checks, {"authorization": "Bearer bad.jwt"}) == (401, False)

    def test_no_checks_without_config(self, monkeypatch):
        monkeypatch.delenv("POLYQL_MCP_TOKEN", raising=False)
        monkeypatch.delenv("JWT_SECRET", raising=False)
        assert srv._token_checks() == []

    def test_non_http_scopes_pass_through(self):
        reached = []

        async def app(scope, receive, send):
            reached.append(scope["type"])

        _run(lambda: srv.BearerAuth(app, [lambda t: False])({"type": "lifespan"}, None, None))
        assert reached == ["lifespan"]
