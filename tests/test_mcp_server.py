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
    with patch.object(srv, "_known_tables", return_value=dict(KNOWN)) as known:
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
                         "find_join_path", "get_business_terms", "validate_sql"}
        assert prompts == {"write_sql"}
        assert "search_schema" in instructions

    def test_validate_sql_publishes_output_schema(self):
        async def go():
            async with Client(srv.mcp) as client:
                return {t.name: t for t in (await client.list_tools()).tools}["validate_sql"]
        tool = _run(go)
        assert set(tool.output_schema["properties"]) == {
            "valid", "read_only", "statement_types", "unknown_tables", "errors", "warnings"}

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
                          "unknown_tables": [], "errors": [], "warnings": []}

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

    def test_empty_and_garbage(self, known_tables):
        assert self.check("   ")["errors"] == ["No SQL statement found."]
        assert not self.check("selec x frm y")["valid"]

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

    def test_streamable_http_binds_localhost_by_default(self):
        with patch.object(srv.mcp, "run") as run:
            srv.main(["--transport", "streamable-http", "--port", "9000"])
        run.assert_called_once_with("streamable-http", host="127.0.0.1", port=9000)
