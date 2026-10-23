"""
Tests for Apache Ossie (formerly OSI) import / export — MetadataManager/OssieStore.py
and backend/semantic.py.

Storage (SQLite, ChromaDB, Kuzu, glossary) is mocked; mapping and the
round-trip are tested on real documents.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml

from MetadataManager import OssieStore as O

DOC = """
version: 0.2.0.dev0
name: retail
ai_context: {instructions: Revenue excludes returns.}
datasets:
  - name: store_sales
    source: retail.public.store_sales
    primary_key: [ticket_number, item_sk]
    description: One row per item per ticket.
    fields:
      - {name: ticket_number, expression: ticket_number, datatype: Integer}
      - {name: item_sk, expression: item_sk, datatype: Integer}
      - {name: customer_sk, expression: customer_sk, datatype: Integer}
      - name: net_paid
        expression: {dialects: [{dialect: SNOWFLAKE, expression: x}, {dialect: ANSI_SQL, expression: ss_net_paid}]}
        datatype: Decimal
        ai_context: {synonyms: [sales amount]}
  - name: customer
    source: retail.public.customer
    primary_key: [customer_sk]
    unique_keys: [[customer_id], [first_name, last_name]]
    ai_context: Use for demographics.
    fields:
      - {name: customer_sk, expression: customer_sk, datatype: Integer}
      - {name: customer_id, expression: customer_id, datatype: String}
  - name: recent
    source: "SELECT * FROM retail.public.store_sales WHERE sold_date_sk > 100"
    fields:
      - {name: ticket_number, expression: ticket_number}
relationships:
  - {name: sales_customer, from: store_sales, to: customer, from_columns: [customer_sk], to_columns: [customer_sk]}
  - {name: broken, from: store_sales, to: nowhere, from_columns: [x], to_columns: [y]}
  - {name: uneven, from: store_sales, to: customer, from_columns: [a, b], to_columns: [c]}
metrics:
  - name: total_net_paid
    expression: {dialects: [{dialect: ANSI_SQL, expression: "SUM(store_sales.net_paid)"}]}
    description: Revenue net of discounts.
    ai_context: {synonyms: [revenue]}
  - {name: no_expr}
"""


@pytest.fixture()
def mapped():
    return O.model_to_polyql(O.load_models(DOC)[0])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParsing:
    def test_current_and_legacy_wrapper(self):
        assert len(O.load_models(DOC)) == 1
        legacy = {"semantic_model": [yaml.safe_load(DOC), {"name": "b", "datasets": [{"name": "t"}]}]}
        assert [m["name"] for m in O.load_models(json.dumps(legacy))] == ["retail", "b"]

    @pytest.mark.parametrize("text", ["[1, 2]", "name: x", "datasets: []", "{bad: [yaml", "semantic_model: [{}]"])
    def test_invalid_documents(self, text):
        with pytest.raises(ValueError):
            O.load_models(text)

    def test_expression_text(self):
        assert O.expression_text("a + b") == "a + b"
        assert O.expression_text({"dialects": [{"dialect": "SNOWFLAKE", "expression": "x"},
                                               {"dialect": "ansi_sql", "expression": "y"}]}) == "y"
        assert O.expression_text({"dialects": [{"dialect": "SNOWFLAKE", "expression": "x"}]}) == "x"
        assert O.expression_text(None) == ""

    def test_ai_context(self):
        assert O.ai_context("plain") == ("plain", [])
        assert O.ai_context({"instructions": "i", "synonyms": "one"}) == ("i", ["one"])
        assert O.ai_context(None) == ("", [])


# ---------------------------------------------------------------------------
# Ossie → PolyQL
# ---------------------------------------------------------------------------

class TestMapping:
    def test_tables_and_keys(self, mapped):
        tables = {t["name"]: t for t in mapped["tables"]}
        assert set(tables) == {"retail.public.store_sales", "retail.public.customer", "recent"}
        sales = {c["name"]: c for c in tables["retail.public.store_sales"]["columns"]}
        # composite key parts are not unique on their own
        assert sales["ticket_number"]["constraints"] == "COMPOSITE KEY PART"
        customer = {c["name"]: c for c in tables["retail.public.customer"]["columns"]}
        assert customer["customer_sk"]["constraints"] == "PRIMARY KEY"
        assert customer["customer_id"]["constraints"] == "UNIQUE"   # single-column unique key only
        assert tables["retail.public.customer"]["desc"] == "Use for demographics."

    def test_expressions_and_synonyms(self, mapped):
        sales = {c["name"]: c for c in mapped["tables"][0]["columns"]}
        assert sales["net_paid"]["source_expr"] == "ss_net_paid"
        assert sales["net_paid"]["logic_type"] == "expression"
        assert sales["net_paid"]["desc"] == "Synonyms: sales amount."
        assert "source_expr" not in sales["item_sk"]   # trivial expression

    def test_relationships_and_skips(self, mapped):
        assert mapped["relationships"] == [{
            "source": "retail.public.store_sales", "target": "retail.public.customer",
            "join_keys": "retail.public.store_sales.customer_sk = retail.public.customer.customer_sk",
            "kind": "fk",
        }]
        skipped = " | ".join(mapped["skipped"])
        assert "broken" in skipped and "uneven" in skipped and "no_expr" in skipped
        assert "recent: source is a query" in skipped and "ai_context" not in skipped

    def test_metrics(self, mapped):
        assert mapped["terms"] == [{
            "term_name": "total_net_paid", "full_name": "", "definition": "Revenue net of discounts.",
            "formula": "SUM(store_sales.net_paid)", "formula_type": "sql",
            "table_deps": ["retail.public.store_sales"], "synonyms": ["revenue"], "domain": "retail",
        }]


# ---------------------------------------------------------------------------
# PolyQL → Ossie
# ---------------------------------------------------------------------------

def _tables():
    return {
        "orders": {"desc": "Orders", "columns": [
            ("id", "INTEGER", "PRIMARY KEY", "Order id"), ("customer_id", "bigint", "", ""),
            ("placed_at", "timestamp with time zone", "", ""), ("total", "numeric(10,2)", "", "", "price*qty")]},
        "customers": {"desc": "", "columns": [("id", "int", "PRIMARY KEY", ""), ("email", "varchar", "UNIQUE", "")]},
        "tags": {"desc": "", "columns": [("name", "text", "", "")]},
    }


class TestExport:
    def model(self, edges, terms=()):
        unique = {("orders", "id"), ("customers", "id"), ("customers", "email")}
        return O.build_model("shop", _tables(), edges, unique, list(terms))

    def test_datasets(self):
        doc = self.model([])
        orders = next(d for d in doc["datasets"] if d["name"] == "orders")
        assert orders["primary_key"] == ["id"] and orders["description"] == "Orders"
        types = {f["name"]: f["datatype"] for f in orders["fields"]}
        assert types == {"id": "Integer", "customer_id": "Integer", "placed_at": "DateTimeTz", "total": "Decimal"}
        exprs = {f["name"]: O.expression_text(f["expression"]) for f in orders["fields"]}
        assert exprs["total"] == "price*qty" and exprs["id"] == "id"
        customers = next(d for d in doc["datasets"] if d["name"] == "customers")
        assert customers["unique_keys"] == [["email"]]
        assert doc["version"] == O.EXPORT_VERSION

    def test_relationship_direction_follows_many_side(self):
        # stored as customers → orders: exporter must flip it so `from` is the many side
        doc = self.model([("customers", "orders", "orders.customer_id = customers.id"),
                          ("orders", "customers", "orders.customer_id = customers.id")])
        assert doc["relationships"] == [{"name": "orders_to_customers", "from": "orders", "to": "customers",
                                         "from_columns": ["customer_id"], "to_columns": ["id"]}]

    def test_many_to_many_goes_to_custom_extensions(self):
        doc = self.model([("orders", "tags", "orders.tag = tags.name")])
        assert "relationships" not in doc
        ext = doc["custom_extensions"][0]
        assert ext["vendor_name"] == "polyql"
        assert json.loads(ext["data"]) == {"joins_not_expressible_as_relationships":
                                           [{"tables": ["orders", "tags"], "join_keys": "orders.tag = tags.name"}]}

    def test_metrics_with_json_synonyms(self):
        doc = self.model([], [{"term_name": "aov", "formula": "AVG(orders.total)", "definition": "Avg order",
                               "synonyms": '["average order value"]'}, {"term_name": "no_formula"}])
        assert doc["metrics"] == [{"name": "aov", "description": "Avg order",
                                   "expression": O._expression("AVG(orders.total)"),
                                   "ai_context": {"synonyms": ["average order value"]}}]

    @pytest.mark.parametrize("sql_type,expected", [
        ("VARCHAR(20)", "String"), ("date", "Date"), ("datetime", "DateTime"), ("time", "Time"),
        ("boolean", "Boolean"), ("double precision", "Float"), ("geometry", "Opaque"), ("Integer", "Integer"),
        ("DateTimeTz", "DateTimeTz"), ("", ""),
    ])
    def test_datatypes(self, sql_type, expected):
        assert O.ossie_datatype(sql_type) == expected


def test_round_trip_is_lossless(mapped):
    tables = {t["name"]: {"desc": t["desc"], "columns": [
        (c["name"], c["type"], c["constraints"], c["desc"], c.get("source_expr", "")) for c in t["columns"]]}
        for t in mapped["tables"]}
    edges = [(r["source"], r["target"], r["join_keys"]) for r in mapped["relationships"]]
    unique = {(t["name"], c["name"]) for t in mapped["tables"] for c in t["columns"]
              if c["constraints"] in ("PRIMARY KEY", "UNIQUE")}
    again = O.model_to_polyql(O.load_models(yaml.safe_dump(
        O.build_model("retail", tables, edges, unique, mapped["terms"])))[0])

    def key(m):
        return (sorted((t["name"], tuple((c["name"], c["type"], c["constraints"], c.get("source_expr", ""))
                                         for c in t["columns"])) for t in m["tables"]),
                sorted(r["join_keys"] for r in m["relationships"]),
                [(t["term_name"], t["formula"], t["synonyms"]) for t in m["terms"]])
    assert key(again) == key(mapped)


# ---------------------------------------------------------------------------
# Storage side
# ---------------------------------------------------------------------------

class TestModelNotes:
    def test_note_from_object_ai_context(self, mapped):
        (note,) = mapped["notes"]
        assert note["concept_id"] == "ossie/retail" and note["type"] == "note"
        assert note["title"] == "retail — guidance" and note["body"] == "Revenue excludes returns."
        assert note["links"] == [] and note["status"] == "stable"
        assert note["tables"] == ["retail.public.store_sales", "retail.public.customer", "recent"]

    def test_string_ai_context_and_synonyms(self):
        def note(ctx):
            model = {"name": "m", "datasets": [{"name": "a"}], "ai_context": ctx}
            return O.model_to_polyql(model)["notes"]
        assert note("Plain text")[0]["body"] == "Plain text"
        body = note({"instructions": "Do x.", "synonyms": ["s1", "s2"]})[0]["body"]
        assert body == "Do x.\n\nSynonyms: s1, s2."
        assert note({"synonyms": ["only"]})[0]["body"] == "Synonyms: only."

    @pytest.mark.parametrize("ctx", [None, "", "  ", {}, {"instructions": ""}])
    def test_no_note_without_ai_context(self, ctx):
        model = {"name": "m", "datasets": [{"name": "a"}], "ai_context": ctx}
        assert O.model_to_polyql(model)["notes"] == []

    def test_note_reaches_table_search(self, mapped):
        from MetadataManager import OKFStore
        concept = mapped["notes"][0]
        names = OKFStore.concept_table_candidates(concept, {})
        known = ["store_sales", "customer", "recent", "other"]
        assert OKFStore.resolve_tables(names, known) == ["store_sales", "customer", "recent"]


class TestImportIntoPolyQL:
    def _run(self, **kwargs):
        with patch("backend.ingestion.store_table"), \
             patch("MetadataManager.MetadataStore.ManageRelations.Relations"), \
             patch("MetadataManager.GlossaryStore.get_term_by_name", return_value=None), \
             patch("MetadataManager.GlossaryStore.add_term", return_value="new"), \
             patch("MetadataManager.GlossaryStore.get_term", return_value={}):
            return O.import_into_polyql(DOC, instance_name="prod", **kwargs)

    def test_okf_index_receives_notes(self):
        index = MagicMock()
        summary = self._run(okf_index=index)
        assert summary["notes"] == 1 and not any("ai_context" in m for m in summary["skipped"])
        notes, bundle = index.call_args.args
        assert bundle == "ossie:retail" and notes[0]["concept_id"] == "ossie/retail"

    def test_reimport_without_ai_context_clears_note(self):
        index = MagicMock()
        with patch("MetadataManager.OssieStore.load_models", return_value=[{"name": "retail", "datasets": []}]):
            summary = self._run(okf_index=index)
        assert summary["notes"] == 0
        index.assert_called_once_with([], "ossie:retail")

    def test_without_okf_index_note_is_skipped(self):
        summary = self._run()
        assert summary["notes"] == 0
        assert any("retail: ai_context not imported" in m for m in summary["skipped"])

    def test_stores_tables_edges_and_upserts_terms(self):
        rag, index = MagicMock(), MagicMock()
        existing = {"term_id": "t1", "term_name": "total_net_paid"}
        relations = MagicMock()
        with patch("backend.ingestion.store_table") as store, \
             patch("MetadataManager.MetadataStore.ManageRelations.Relations", return_value=relations) as rel_cls, \
             patch("MetadataManager.GlossaryStore.get_term_by_name", return_value=existing), \
             patch("MetadataManager.GlossaryStore.update_term") as update, \
             patch("MetadataManager.GlossaryStore.add_term") as add, \
             patch("MetadataManager.GlossaryStore.get_term", return_value={"term_id": "t1"}):
            summary = O.import_into_polyql(DOC, instance_name="prod", db_type="snowflake",
                                           vdb=rag, glossary_index=index)

        assert summary["tables"] == 3 and summary["relationships"] == 1 and summary["metrics"] == 1
        assert summary["models"] == ["retail"]
        kwargs = store.call_args_list[0].kwargs
        assert kwargs == {"instance_name": "prod", "db_type": "snowflake", "vdb": rag, "replace": True}
        rel_cls.assert_called_once_with(instance_name="prod")
        assert relations.addRelation.call_args[0][0][0][3] == "fk"
        update.assert_called_once()
        add.assert_not_called()
        index.assert_called_once_with({"term_id": "t1"})

    def test_new_term_is_added(self):
        with patch("backend.ingestion.store_table"), \
             patch("MetadataManager.MetadataStore.ManageRelations.Relations"), \
             patch("MetadataManager.GlossaryStore.get_term_by_name", return_value=None), \
             patch("MetadataManager.GlossaryStore.add_term", return_value="new") as add, \
             patch("MetadataManager.GlossaryStore.get_term", return_value={}):
            O.import_into_polyql(DOC, instance_name="prod")
        assert add.call_args[0][0]["instance_name"] == "prod"


def test_store_table_replace_and_shared_vdb():
    from backend import ingestion

    db, vdb = MagicMock(), MagicMock()
    with patch("Utilities.base_utils.accessDB", return_value=db), \
         patch("MetadataManager.MetadataBuilder.importExisting.importData.importDD"), \
         patch("MetadataManager.MetadataStore.RAGPipeline.ManageInformation") as fresh, \
         patch("MetadataManager.MetadataStore.relationdb.kuzuDB._get_conn"), \
         patch("MetadataManager.MetadataStore.relationdb.kuzuDB._ensure_schema"), \
         patch("MetadataManager.MetadataStore.relationdb.kuzuDB._merge_node"):
        ingestion.store_table("orders", "d", [{"name": "total", "source_expr": "p*q", "logic_type": "expression"}],
                              [], instance_name="prod", vdb=vdb, replace=True)
    deleted = [c.args for c in db.delete_data.call_args_list]
    assert ("tableDesc", {"tableName": "orders", "instance_name": "prod"}) in deleted
    assert ("tableColMetadata", {"TableName": "orders", "instance_name": "prod"}) in deleted
    fresh.assert_not_called()
    vdb.add_new_data.assert_called_once()
    record = db.post_data.call_args_list[1].args[1][0]
    assert record["logic"] == "p*q" and record["type_of_logic"] == "expression"


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

class TestEndpoints:
    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient

        from backend.app import app
        return TestClient(app)

    def test_import(self, client):
        summary = {"models": ["retail"], "tables": 3, "relationships": 1, "metrics": 1, "notes": 1, "skipped": []}
        with patch("MetadataManager.MetadataStore.RAGPipeline.ManageInformation") as rag_cls, \
             patch("MetadataManager.OKFStore.index_concepts") as index, \
             patch("MetadataManager.OssieStore.import_into_polyql", return_value=summary) as imp:
            resp = client.post("/api/semantic/import/ossie", json={"content": DOC, "instance_name": "prod"})
        assert resp.status_code == 200 and resp.json() == summary
        assert imp.call_args.kwargs["instance_name"] == "prod"
        imp.call_args.kwargs["okf_index"]([{"concept_id": "ossie/retail"}], "ossie:retail")
        rag = rag_cls.return_value
        index.assert_called_once_with([{"concept_id": "ossie/retail"}], rag.embedding_model,
                                      rag.vdb.client, "prod", "ossie:retail")

    def test_import_rejects_invalid_document_before_loading_models(self, client):
        with patch("MetadataManager.MetadataStore.RAGPipeline.ManageInformation") as rag:
            resp = client.post("/api/semantic/import/ossie", json={"content": "name: x"})
        assert resp.status_code == 400 and "datasets" in resp.json()["detail"]
        rag.assert_not_called()

    def test_export(self, client):
        with patch("MetadataManager.OssieStore.build_document", return_value="version: 0.2.0.dev0\n") as build:
            resp = client.get("/api/semantic/export/ossie?instance_name=prod")
        assert resp.status_code == 200 and resp.text.startswith("version:")
        assert resp.headers["content-type"].startswith("application/yaml")
        assert 'filename="polyql_prod.ossie.yaml"' in resp.headers["content-disposition"]
        build.assert_called_once_with("prod", None)

    def test_requires_auth(self, client):
        from backend.app import app
        from backend.auth import get_current_user

        override = app.dependency_overrides.pop(get_current_user)
        try:
            assert client.get("/api/semantic/export/ossie").status_code == 401
        finally:
            app.dependency_overrides[get_current_user] = override
