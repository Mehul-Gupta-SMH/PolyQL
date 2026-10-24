"""
Tests for OKF (Open Knowledge Format) support in table search.

Coverage:
  - load_bundle / parse_concept: frontmatter, reserved files, hidden dirs, non-conforming docs, links
  - table_candidates / resolve_tables: resource URIs, titles, qualified names
  - import_bundle: one-hop link expansion, deprecated concepts, stable IDs
  - get_okf_tables: distance cutoff, instance filter, unknown tables, error fallback
  - SQLBuilderSupport.__addOKFTables__: adds OKF tables after vector results, config gating
  - POST /api/okf/import: auth + error mapping

ChromaDB and the embedding model are mocked — no real model or vector DB required.
"""

from unittest.mock import MagicMock, patch

import pytest

from MetadataManager import OKFStore


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def bundle(tmp_path):
    """A small OKF v0.2 bundle: two table concepts, a metric linking to one, plus edge cases."""
    _write(tmp_path, "index.md", "---\nokf_version: \"0.2\"\n---\n# Tables\n- [orders](/tables/orders.md)\n")
    _write(tmp_path, "log.md", "# 2026-10-01\n- **Update** added orders\n")
    _write(tmp_path, "tables/orders.md", (
        "---\n"
        "type: BigQuery Table\n"
        "title: Customer Orders\n"
        "description: One row per order placed.\n"
        "resource: https://console.cloud.google.com/bigquery?p=acme&d=sales&t=orders\n"
        "tags: [sales, orders]\n"
        "---\n"
        "# Joins\nJoins [customers](./customers.md) on `customer_id`.\n"
    ))
    _write(tmp_path, "tables/customers.md", "---\ntype: Table\ntitle: Customers\n---\nOne row per customer.\n")
    _write(tmp_path, "metrics/revenue.md", (
        "---\ntype: Metric\ntitle: Revenue\ndescription: Recognised revenue.\n---\n"
        "Sum of delivered orders. Joins on [orders](/tables/orders.md). "
        "See [policy](https://example.com/policy) and [notes](#definition).\n"
    ))
    _write(tmp_path, "metrics/old_revenue.md", "---\ntype: Metric\ntitle: Old revenue\nstatus: deprecated\n---\nOld.\n")
    _write(tmp_path, "notes/broken.md", "no frontmatter here\n")
    _write(tmp_path, "notes/no_type.md", "---\ntitle: Missing type\n---\nBody\n")
    _write(tmp_path, ".git/ignored.md", "---\ntype: Note\n---\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestLoadBundle:
    def test_loads_conforming_concepts_and_skips_rest(self, bundle):
        concepts, skipped = OKFStore.load_bundle(str(bundle))
        ids = {c["concept_id"] for c in concepts}

        assert ids == {"/tables/orders", "/tables/customers", "/metrics/revenue", "/metrics/old_revenue"}
        assert set(skipped) == {"/notes/broken", "/notes/no_type"}

    def test_parses_frontmatter_fields(self, bundle):
        concepts, _ = OKFStore.load_bundle(str(bundle))
        orders = next(c for c in concepts if c["concept_id"] == "/tables/orders")

        assert orders["type"] == "BigQuery Table"
        assert orders["title"] == "Customer Orders"
        assert orders["tags"] == ["sales", "orders"]
        assert orders["status"] == "stable"

    def test_links_resolve_relative_and_absolute_and_skip_external(self, bundle):
        concepts, _ = OKFStore.load_bundle(str(bundle))
        by_id = {c["concept_id"]: c for c in concepts}

        assert by_id["/tables/orders"]["links"] == ["/tables/customers"]
        assert by_id["/metrics/revenue"]["links"] == ["/tables/orders"]

    def test_missing_directory_raises(self, tmp_path):
        with pytest.raises(ValueError):
            OKFStore.load_bundle(str(tmp_path / "nope"))

    def test_title_defaults_to_file_name(self):
        concept = OKFStore.parse_concept("---\ntype: Table\n---\n", "/tables/line_items")
        assert concept["title"] == "line_items"


# ---------------------------------------------------------------------------
# Concept → table mapping
# ---------------------------------------------------------------------------

class TestTableMapping:
    def test_table_candidates_from_resource_title_and_file(self, bundle):
        concepts, _ = OKFStore.load_bundle(str(bundle))
        orders = next(c for c in concepts if c["concept_id"] == "/tables/orders")

        assert OKFStore.table_candidates(orders) == ["orders", "bigquery", "customer_orders"]

    def test_explicit_table_key_comes_first(self):
        concept = OKFStore.parse_concept("---\ntype: View\ntable: sales.v_orders\n---\n", "/views/x")
        assert OKFStore.table_candidates(concept)[0] == "sales.v_orders"

    def test_non_table_concept_has_no_own_candidates(self):
        concept = OKFStore.parse_concept("---\ntype: Metric\n---\n", "/metrics/revenue")
        assert OKFStore.table_candidates(concept) == []

    def test_metric_inherits_linked_table_candidates(self, bundle):
        concepts, _ = OKFStore.load_bundle(str(bundle))
        by_id = {c["concept_id"]: c for c in concepts}

        assert "orders" in OKFStore.concept_table_candidates(by_id["/metrics/revenue"], by_id)

    def test_resolve_tables_exact_and_qualified(self):
        known = ["sales.public.orders", "Customers", "dim_date"]

        assert OKFStore.resolve_tables(["orders"], known) == ["sales.public.orders"]
        assert OKFStore.resolve_tables(["customers"], known) == ["Customers"]
        assert OKFStore.resolve_tables(["proj.ds.dim_date"], known) == ["dim_date"]
        assert OKFStore.resolve_tables(["bigquery", "unknown"], known) == []


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _model():
    model = MagicMock()
    model.encode.return_value.tolist.return_value = [0.1, 0.2]
    return model


class TestImportBundle:
    def test_indexes_active_concepts_with_table_candidates(self, bundle):
        with patch("MetadataManager.MetadataStore.vdb.Chroma.addData") as add_data:
            summary = OKFStore.import_bundle(str(bundle), _model(), MagicMock(), instance_name="prod")

        assert summary == {
            "bundle": bundle.name,   # defaults to the bundle directory's name
            "indexed": 3, "skipped": ["/notes/broken", "/notes/no_type"],
            "deprecated": 1, "table_concepts": 2,
        }
        records = add_data.call_args[0][1]
        meta = {r["metadata"]["concept_id"]: r["metadata"] for r in records}
        assert meta["/metrics/revenue"]["tables"].split(",")[0] == "orders"
        assert all(m["instance_name"] == "prod" for m in meta.values())
        assert add_data.call_args[0][2]["collection_name"] == "okf_concepts"

    def test_ids_are_stable_across_imports(self, bundle):
        with patch("MetadataManager.MetadataStore.vdb.Chroma.addData") as add_data:
            OKFStore.import_bundle(str(bundle), _model(), MagicMock())
            OKFStore.import_bundle(str(bundle), _model(), MagicMock())

        first, second = (sorted(r["id"] for r in call[0][1]) for call in add_data.call_args_list)
        assert first == second


def _concept_dict(cid, status="stable", **extra):
    return {"concept_id": cid, "type": "note", "title": cid, "description": "", "resource": "",
            "tags": [], "status": status, "table_name": "", "body": "b", "links": [], **extra}


class TestIndexConcepts:
    def test_skips_deprecated_and_replaces_bundle_before_add(self):
        order, client = [], MagicMock()
        concepts = [_concept_dict("a", tables=["Orders"]), _concept_dict("b", status="deprecated")]
        with patch("MetadataManager.OKFStore.delete_bundle",
                   side_effect=lambda *a: order.append("delete")) as delete, \
             patch("MetadataManager.MetadataStore.vdb.Chroma.addData",
                   side_effect=lambda *a: order.append("add")) as add_data, \
             patch("MetadataManager.retrieval_cache.clear") as clear:
            result = OKFStore.index_concepts(concepts, _model(), client, "prod", "ossie:m")

        assert result == {"bundle": "ossie:m", "indexed": 1, "deprecated": 1, "table_concepts": 0}
        assert order == ["delete", "add"]
        delete.assert_called_once_with(client, "prod", "ossie:m")
        (record,) = add_data.call_args[0][1]
        assert record["metadata"]["tables"] == "orders" and record["metadata"]["bundle"] == "ossie:m"
        clear.assert_called_once()

    def test_empty_bundle_only_deletes_and_clears_cache_on_error(self):
        with patch("MetadataManager.OKFStore.delete_bundle", side_effect=RuntimeError("boom")), \
             patch("MetadataManager.retrieval_cache.clear") as clear, \
             pytest.raises(RuntimeError):
            OKFStore.index_concepts([], _model(), MagicMock())
        clear.assert_called_once()

    def test_explicit_tables_in_candidates(self):
        concept = _concept_dict("n", tables=["Sales Orders", "orders"])
        assert OKFStore.concept_table_candidates(concept, {}) == ["sales_orders", "orders"]


# ---------------------------------------------------------------------------
# Query-time search
# ---------------------------------------------------------------------------

def _raw(*hits):
    """Fake ChromaDB query result from (metadata, distance) pairs."""
    return {"metadatas": [[h[0] for h in hits]], "distances": [[h[1] for h in hits]]}


class TestGetOKFTables:
    KNOWN = ["orders", "customers", "dim_date"]

    def test_returns_resolved_tables_within_distance(self):
        raw = _raw(
            ({"concept_id": "/metrics/revenue", "title": "Revenue", "tables": "orders,missing"}, 0.2),
            ({"concept_id": "/tables/customers", "title": "Customers", "tables": "customers"}, 0.9),
        )
        with patch("MetadataManager.MetadataStore.vdb.Chroma.getData", return_value=raw):
            hits = OKFStore.get_okf_tables("revenue", self.KNOWN, _model(), MagicMock(), max_distance=0.5)

        assert [h["table"] for h in hits] == ["orders"]
        assert hits[0]["concept_id"] == "/metrics/revenue"

    def test_best_concept_wins_and_tables_are_unique(self):
        raw = _raw(
            ({"concept_id": "/b", "tables": "orders"}, 0.4),
            ({"concept_id": "/a", "tables": "orders,customers"}, 0.1),
        )
        with patch("MetadataManager.MetadataStore.vdb.Chroma.getData", return_value=raw):
            hits = OKFStore.get_okf_tables("q", self.KNOWN, _model(), MagicMock())

        assert [(h["table"], h["concept_id"]) for h in hits] == [("orders", "/a"), ("customers", "/a")]

    def test_instance_filter_only_for_named_instances(self):
        with patch("MetadataManager.MetadataStore.vdb.Chroma.getData", return_value=_raw()) as get_data:
            OKFStore.get_okf_tables("q", self.KNOWN, _model(), MagicMock(), instance_name="prod")
            OKFStore.get_okf_tables("q", self.KNOWN, _model(), MagicMock(), instance_name="default")

        assert get_data.call_args_list[0].kwargs == {"instance_name": "prod"}
        assert get_data.call_args_list[1].kwargs == {}

    def test_missing_collection_returns_empty(self):
        with patch("MetadataManager.MetadataStore.vdb.Chroma.getData", side_effect=ValueError("no collection")):
            assert OKFStore.get_okf_tables("q", self.KNOWN, _model(), MagicMock()) == []


# ---------------------------------------------------------------------------
# Table-search integration
# ---------------------------------------------------------------------------

def _support(instance_name="default"):
    from SQLBuilderComponents import SQLBuilderSupport

    obj = SQLBuilderSupport.__new__(SQLBuilderSupport)
    obj.user_query = "monthly revenue"
    obj.instance_name = instance_name
    obj.table_list = {"direct": {"customers": {"description": "vector hit", "columns": {}}}, "intermediate": {}}
    obj.tmddb_config = {"tableDescName": "tableDesc"}
    obj.DBObj = MagicMock()
    obj.DBObj.get_data.return_value = [("orders", "Order facts"), ("customers", "Customer dim")]
    return obj


def _rag():
    rag = MagicMock()
    rag.embedding_model = _model()
    return rag


def _concept(cid, tables, body="", distance=0.2, **extra):
    return {"concept_id": cid, "title": cid.rsplit("/", 1)[-1], "type": "Metric",
            "description": "", "body": body, "tables": tables, "distance": distance, **extra}


class TestAddOKFTables:
    CFG = {"enabled": True, "top_k": 5, "max_distance": 0.4}

    def test_adds_okf_tables_after_vector_hits(self):
        obj = _support()
        concepts = [_concept("/c", ["customers"], distance=0.1), _concept("/metrics/revenue", ["orders"])]
        with patch("SQLBuilderComponents.get_config_val", return_value=self.CFG), \
             patch("MetadataManager.OKFStore.search_concepts", return_value=concepts) as search:
            obj.__addOKFTables__(_rag())

        assert list(obj.table_list["direct"]) == ["customers", "orders"]
        assert obj.table_list["direct"]["customers"]["description"] == "vector hit"
        assert obj.table_list["direct"]["orders"]["description"] == "Order facts"
        assert search.call_args.kwargs["top_k"] == 5
        assert search.call_args.kwargs["max_distance"] == 0.4

    def test_known_tables_scoped_to_instance(self):
        obj = _support(instance_name="prod")
        with patch("SQLBuilderComponents.get_config_val", return_value=self.CFG), \
             patch("MetadataManager.OKFStore.search_concepts", return_value=[]):
            obj.__addOKFTables__(_rag())

        assert obj.DBObj.get_data.call_args[0][1] == {"instance_name": "prod"}

    @pytest.mark.parametrize("cfg", [{"enabled": False}, KeyError("okf")])
    def test_disabled_or_missing_config_is_noop(self, cfg):
        obj = _support()
        side = cfg if isinstance(cfg, Exception) else None
        with patch("SQLBuilderComponents.get_config_val", return_value=cfg, side_effect=side), \
             patch("MetadataManager.OKFStore.search_concepts") as search:
            obj.__addOKFTables__(_rag())

        search.assert_not_called()
        assert list(obj.table_list["direct"]) == ["customers"]
        assert obj.okf_hits == []

    def test_keeps_concepts_for_prompt_including_tableless_ones(self):
        obj = _support()
        concepts = [
            _concept("/metrics/revenue", ["orders"], body="Net of returns."),
            _concept("/playbooks/fiscal_year", [], body="Fiscal year starts in April."),
        ]
        with patch("SQLBuilderComponents.get_config_val", return_value=self.CFG), \
             patch("MetadataManager.OKFStore.search_concepts", return_value=concepts):
            obj.__addOKFTables__(_rag())

        assert [h["concept_id"] for h in obj.okf_hits] == ["/metrics/revenue", "/playbooks/fiscal_year"]
        assert obj.okf_hits[1]["body"] == "Fiscal year starts in April."

    def test_prompt_bodies_are_truncated_on_word_boundary(self):
        obj = _support()
        concepts = [_concept("/n", [], body="alpha beta gamma delta epsilon")]
        cfg = {**self.CFG, "prompt_chars": 14}
        with patch("SQLBuilderComponents.get_config_val", return_value=cfg), \
             patch("MetadataManager.OKFStore.search_concepts", return_value=concepts):
            obj.__addOKFTables__(_rag())

        assert obj.okf_hits[0]["body"] == "alpha beta …"
        assert concepts[0]["body"] == "alpha beta gamma delta epsilon"  # caller's data untouched

    def test_include_in_prompt_false_keeps_tables_but_no_notes(self):
        obj = _support()
        concepts = [_concept("/metrics/revenue", ["orders"], body="x")]
        cfg = {**self.CFG, "include_in_prompt": False}
        with patch("SQLBuilderComponents.get_config_val", return_value=cfg), \
             patch("MetadataManager.OKFStore.search_concepts", return_value=concepts):
            obj.__addOKFTables__(_rag())

        assert "orders" in obj.table_list["direct"]
        assert obj.okf_hits == []


class TestSearchConcepts:
    def test_returns_concepts_with_resolved_tables_and_body(self):
        raw = {
            "metadatas": [[
                {"concept_id": "/playbooks/fy", "title": "Fiscal year", "type": "Playbook",
                 "tables": "", "body": "Starts in April."},
                {"concept_id": "/metrics/revenue", "title": "Revenue", "tables": "orders,missing",
                 "body": "Net of returns."},
            ]],
            "distances": [[0.3, 0.1]],
            "documents": [["doc fy", "doc revenue"]],
        }
        with patch("MetadataManager.MetadataStore.vdb.Chroma.getData", return_value=raw):
            concepts = OKFStore.search_concepts("q", ["orders"], _model(), MagicMock())

        assert [c["concept_id"] for c in concepts] == ["/metrics/revenue", "/playbooks/fy"]
        assert concepts[0]["tables"] == ["orders"] and concepts[0]["body"] == "Net of returns."
        assert concepts[1]["tables"] == []

    def test_falls_back_to_document_for_older_imports(self):
        raw = {"metadatas": [[{"concept_id": "/a", "tables": ""}]], "distances": [[0.1]],
               "documents": [["Revenue\nMetric\nNet of returns."]]}
        with patch("MetadataManager.MetadataStore.vdb.Chroma.getData", return_value=raw):
            concepts = OKFStore.search_concepts("q", [], _model(), MagicMock())

        assert concepts[0]["body"] == "Revenue\nMetric\nNet of returns."

    def test_import_stores_body_excerpt(self, bundle):
        with patch("MetadataManager.MetadataStore.vdb.Chroma.addData") as add_data:
            OKFStore.import_bundle(str(bundle), _model(), MagicMock())

        meta = {r["metadata"]["concept_id"]: r["metadata"] for r in add_data.call_args[0][1]}
        assert meta["/metrics/revenue"]["body"].startswith("Sum of delivered orders.")


class TestPromptKnowledgeNotes:
    def _schema(self, **ctx):
        from APIManager.PromptBuilder import PromptBuilder

        context = {"user_query": "monthly revenue", "table_list": {"direct": {}, "intermediate": {}},
                   "join_keys": [], **ctx}
        return PromptBuilder.format_schema(context)

    def test_renders_knowledge_notes_before_schema(self):
        hit = _concept("/metrics/revenue", ["orders"], body="# Definition\nNet of returns.",
                       description="Recognised revenue.")
        hit.update(title="Revenue", type="Metric")
        out = self._schema(okf_hits=[hit])

        assert "## Knowledge Notes" in out
        assert out.index("## Knowledge Notes") < out.index("## Database Schema")
        assert "### Revenue (Metric)" in out
        assert "> Recognised revenue." in out
        assert "**Tables:** orders" in out
        assert "**Definition**" in out and "\n# Definition" not in out
        assert "Net of returns." in out

    def test_omitted_without_hits(self):
        assert "## Knowledge Notes" not in self._schema()
        assert "## Knowledge Notes" not in self._schema(okf_hits=[])

    def test_follows_business_definitions(self):
        out = self._schema(
            glossary_hits=[{"term_name": "AUM", "definition": "Assets under management."}],
            okf_hits=[_concept("/n", [], body="note")],
        )
        assert out.index("## Business Definitions") < out.index("## Knowledge Notes")


# ---------------------------------------------------------------------------
# REST endpoint
# ---------------------------------------------------------------------------

class TestImportEndpoint:
    # conftest.py overrides get_current_user for every test, so the client is authenticated
    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient

        from backend.app import app

        return TestClient(app)

    def test_requires_auth(self, client):
        from backend.app import app
        from backend.auth import get_current_user

        override = app.dependency_overrides.pop(get_current_user)
        try:
            resp = client.post("/api/okf/import", json={"path": "/tmp"})
        finally:
            app.dependency_overrides[get_current_user] = override
        assert resp.status_code == 401

    def test_import_returns_summary(self, client):
        summary = {"indexed": 3, "skipped": [], "deprecated": 0, "table_concepts": 2}
        with patch("MetadataManager.MetadataStore.RAGPipeline.ManageInformation"), \
             patch("MetadataManager.OKFStore.import_bundle", return_value=summary) as imp:
            resp = client.post("/api/okf/import", json={"path": "/bundles/acme", "instance_name": "prod"})

        assert resp.status_code == 200
        assert resp.json() == summary
        assert imp.call_args.args[0] == "/bundles/acme"
        assert imp.call_args.kwargs["instance_name"] == "prod"

    def test_bad_path_returns_400(self, client):
        with patch("MetadataManager.MetadataStore.RAGPipeline.ManageInformation"), \
             patch("MetadataManager.OKFStore.import_bundle", side_effect=ValueError("not a directory")):
            resp = client.post("/api/okf/import", json={"path": "/nope"})

        assert resp.status_code == 400
        assert "not a directory" in resp.json()["detail"]


def test_bom_prefixed_concept_is_parsed(tmp_path):
    (tmp_path / "orders.md").write_bytes("﻿---\ntype: Table\n---\nBody\n".encode("utf-8"))
    concepts, skipped = OKFStore.load_bundle(str(tmp_path))
    assert [c["concept_id"] for c in concepts] == ["/orders"] and skipped == []


class _FakeCollection:
    """Just enough of a ChromaDB collection for bundle listing/removal."""

    def __init__(self, rows):
        self.rows = dict(rows)  # id -> metadata

    def get(self, where=None, include=None):
        ids = [i for i, m in self.rows.items()
               if not where or all(m.get(k) == v for k, v in where.items())]
        return {"ids": ids, "metadatas": [self.rows[i] for i in ids]}

    def delete(self, ids):
        for i in ids:
            self.rows.pop(i)


def _fake_client(rows):
    client = MagicMock()
    client.get_collection.return_value = _FakeCollection(rows)
    return client


_ROWS = {
    "a": {"instance_name": "default", "bundle": "sales"},
    "b": {"instance_name": "default", "bundle": "sales"},
    "c": {"instance_name": "default", "bundle": "hr"},
    "d": {"instance_name": "prod", "bundle": "sales"},
    "e": {"instance_name": "default"},  # imported before bundles were tracked
}


class TestBundles:
    def test_list_counts_per_instance_and_bundle(self):
        assert OKFStore.list_bundles(_fake_client(_ROWS)) == [
            {"instance_name": "default", "bundle": "(unnamed)", "concepts": 1},
            {"instance_name": "default", "bundle": "hr", "concepts": 1},
            {"instance_name": "default", "bundle": "sales", "concepts": 2},
            {"instance_name": "prod", "bundle": "sales", "concepts": 1},
        ]

    def test_list_without_collection_is_empty(self):
        client = MagicMock()
        client.get_collection.side_effect = ValueError("does not exist")
        assert OKFStore.list_bundles(client) == []
        assert OKFStore.delete_bundle(client, "default", "sales") == 0

    def test_get_bundle_concepts(self):
        client = _fake_client(_ROWS)
        assert [m.get("bundle") for m in OKFStore.get_bundle_concepts(client, "default")] == \
            [None, "hr", "sales", "sales"]
        got = OKFStore.get_bundle_concepts(client, "default", "sales")
        assert len(got) == 2 and all(m["instance_name"] == "default" for m in got)
        assert OKFStore.get_bundle_concepts(client, "default", "(unnamed)") == [_ROWS["e"]]
        assert OKFStore.get_bundle_concepts(client, "nobody") == []

    def test_get_bundle_concepts_without_collection(self):
        client = MagicMock()
        client.get_collection.side_effect = ValueError("does not exist")
        assert OKFStore.get_bundle_concepts(client, "default", "sales") == []

    def test_delete_one_bundle_of_one_instance(self):
        client = _fake_client(_ROWS)
        assert OKFStore.delete_bundle(client, "default", "sales") == 2
        assert set(client.get_collection.return_value.rows) == {"c", "d", "e"}

    def test_delete_unnamed_bundle(self):
        client = _fake_client(_ROWS)
        assert OKFStore.delete_bundle(client, "default", "(unnamed)") == 1
        assert "e" not in client.get_collection.return_value.rows

    def test_delete_all_bundles_of_instance(self):
        client = _fake_client(_ROWS)
        assert OKFStore.delete_bundle(client, "default") == 4
        assert set(client.get_collection.return_value.rows) == {"d"}

    def test_reimport_replaces_bundle_concepts(self, bundle):
        client = _fake_client({"stale": {"instance_name": "default", "bundle": bundle.name},
                               "other": {"instance_name": "default", "bundle": "hr"}})
        with patch("MetadataManager.MetadataStore.vdb.Chroma.addData"):
            OKFStore.import_bundle(str(bundle), _model(), client)
        assert set(client.get_collection.return_value.rows) == {"other"}


def _export_fixture():
    tables = {
        "sales.orders": {"desc": "Orders placed", "columns": [
            ("id", "int", "PRIMARY KEY", "Order id"),
            ("customer_id", "int", "", ""),
            ("total", "numeric", "", "Amount", "price*qty"),
        ]},
        "sales.customers": {"desc": "Customers", "columns": [("id", "int", "PRIMARY KEY", "")]},
    }
    edges = [("sales.orders", "sales.customers", "sales.orders.customer_id = sales.customers.id")]
    unique = {("sales.customers", "id"), ("sales.orders", "id")}
    terms = [{"term_name": "Net Revenue", "definition": "Revenue after refunds",
              "formula": "SUM(sales.orders.total)", "table_deps": '["sales.orders"]',
              "synonyms": '["sales"]'}]
    return tables, edges, unique, terms


class TestExportBundle:
    def test_files_and_frontmatter(self):
        files = OKFStore.export_bundle(*_export_fixture(), "2026-10-20T00:00:00+00:00")
        assert set(files) == {"index.md", "tables/sales.orders.md", "tables/sales.customers.md",
                              "metrics/net_revenue.md"}
        assert 'okf_version: "0.2"' in files["index.md"] or "okf_version: '0.2'" in files["index.md"]
        orders = files["tables/sales.orders.md"]
        assert orders.startswith("---\ntype: Table\n")
        assert "| total | numeric |  | Amount (expression: `price*qty`) |" in orders
        assert "(/tables/sales.customers.md)" in orders and "(n:1)" in orders

    def test_exported_bundle_reimports_onto_the_same_tables(self, tmp_path):
        tables, *rest = _export_fixture()
        for path, text in OKFStore.export_bundle(tables, *rest, "2026-10-20T00:00:00+00:00").items():
            _write(tmp_path, path, text)
        concepts, skipped = OKFStore.load_bundle(str(tmp_path))
        by_id = {c["concept_id"]: c for c in concepts}
        assert skipped == []
        resolved = {
            cid: OKFStore.resolve_tables(OKFStore.concept_table_candidates(c, by_id), list(tables))
            for cid, c in by_id.items()
        }
        assert resolved["/tables/sales.customers"] == ["sales.customers"]
        assert resolved["/metrics/net_revenue"] == ["sales.orders"]
        assert "sales.customers" in resolved["/tables/sales.orders"]


class TestBundleEndpoints:
    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient

        from backend.app import app

        return TestClient(app)

    def test_list(self, client):
        with patch("backend.okf._chroma_client", return_value=_fake_client(_ROWS)):
            resp = client.get("/api/okf/bundles")
        assert resp.status_code == 200
        assert {"instance_name": "prod", "bundle": "sales", "concepts": 1} in resp.json()["bundles"]

    def test_delete(self, client):
        fake = _fake_client(_ROWS)
        with patch("backend.okf._chroma_client", return_value=fake):
            resp = client.delete("/api/okf/bundles", params={"instance_name": "default", "bundle": "hr"})
        assert resp.json() == {"removed": 1}
        assert "c" not in fake.get_collection.return_value.rows

    def test_delete_requires_instance(self, client):
        assert client.delete("/api/okf/bundles").status_code == 422

    def test_export_zip(self, client):
        import io
        import zipfile

        with patch("MetadataManager.OssieStore.read_instance", return_value=_export_fixture()) as read:
            resp = client.get("/api/okf/export", params={"instance_name": "prod"})
        assert resp.status_code == 200
        read.assert_called_once_with("prod")
        assert resp.headers["content-type"] == "application/zip"
        assert 'filename="polyql_prod.okf.zip"' in resp.headers["content-disposition"]
        names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
        assert "polyql_prod/index.md" in names and "polyql_prod/tables/sales.orders.md" in names

    def test_endpoints_require_auth(self, client):
        from backend.app import app
        from backend.auth import get_current_user

        override = app.dependency_overrides.pop(get_current_user)
        try:
            codes = [client.get("/api/okf/bundles").status_code,
                     client.delete("/api/okf/bundles", params={"instance_name": "x"}).status_code,
                     client.get("/api/okf/export").status_code]
        finally:
            app.dependency_overrides[get_current_user] = override
        assert codes == [401, 401, 401]
