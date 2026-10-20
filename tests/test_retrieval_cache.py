"""
Tests for the retrieval cache (MetadataManager/retrieval_cache.py), its
invalidation from every metadata write path, and the shared model/client
accessors (MetadataManager/MetadataStore/shared_models.py).

conftest.py clears the cache and the shared accessors around every test.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

import main
from MetadataManager import retrieval_cache as rc
from MetadataManager.MetadataStore import shared_models


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _ctx(table="orders"):
    return {
        "user_query": "q",
        "table_list": {"direct": {table: {"description": "d", "columns": {}}}, "intermediate": {}},
        "join_keys": {},
        "okf_hits": [],
    }


# ---------------------------------------------------------------------------
# TTLCache
# ---------------------------------------------------------------------------

class TestTTLCache:

    def test_hit_and_miss(self):
        cache = rc.TTLCache()
        assert cache.get("k") is None
        cache.set("k", {"a": 1})
        assert cache.get("k") == {"a": 1}

    def test_ttl_expiry_with_injected_clock(self):
        clock = Clock()
        cache = rc.TTLCache(ttl_seconds=10, clock=clock)
        cache.set("k", 1)
        clock.now += 9.9
        assert cache.get("k") == 1
        clock.now += 0.2
        assert cache.get("k") is None
        assert len(cache) == 0

    def test_lru_eviction(self):
        cache = rc.TTLCache(max_entries=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")          # a is now most recently used
        cache.set("c", 3)       # evicts b
        assert cache.get("b") is None
        assert cache.get("a") == 1
        assert cache.get("c") == 3

    def test_values_are_isolated_both_ways(self):
        cache = rc.TTLCache()
        original = {"tables": ["orders"]}
        cache.set("k", original)
        original["tables"].append("leak-in")
        got = cache.get("k")
        got["tables"].append("leak-out")
        assert cache.get("k") == {"tables": ["orders"]}

    def test_clear(self):
        cache = rc.TTLCache()
        cache.set("k", 1)
        cache.clear()
        assert cache.get("k") is None


# ---------------------------------------------------------------------------
# cached_context
# ---------------------------------------------------------------------------

class TestCachedContext:

    def test_second_call_is_a_hit(self):
        compute = MagicMock(return_value=_ctx())
        rc.cached_context("top customers", "prod", compute)
        rc.cached_context("top customers", "prod", compute)
        assert compute.call_count == 1

    def test_query_whitespace_is_normalised(self):
        compute = MagicMock(return_value=_ctx())
        rc.cached_context("top  customers ", "prod", compute)
        rc.cached_context(" top customers", "prod", compute)
        assert compute.call_count == 1

    def test_key_includes_instance(self):
        compute = MagicMock(side_effect=lambda: _ctx())
        rc.cached_context("q", "prod", compute)
        rc.cached_context("q", "dev", compute)
        assert compute.call_count == 2

    def test_errors_are_not_cached(self):
        compute = MagicMock(side_effect=[RuntimeError("chroma down"), _ctx()])
        with pytest.raises(RuntimeError):
            rc.cached_context("q", "prod", compute)
        assert rc.cached_context("q", "prod", compute)["table_list"]["direct"]
        assert compute.call_count == 2

    def test_empty_results_are_not_cached(self):
        empty = {"table_list": {"direct": {}, "intermediate": {}}, "join_keys": {}}
        compute = MagicMock(return_value=empty)
        rc.cached_context("q", "prod", compute)
        rc.cached_context("q", "prod", compute)
        assert compute.call_count == 2

    def test_mutating_a_result_does_not_corrupt_the_cache(self):
        compute = MagicMock(side_effect=lambda: _ctx())
        first = rc.cached_context("q", "prod", compute)
        first["glossary_hits"] = ["x"]
        first["table_list"]["direct"].clear()
        second = rc.cached_context("q", "prod", compute)
        assert "glossary_hits" not in second
        assert "orders" in second["table_list"]["direct"]
        second["glossary_hits"] = ["y"]
        assert "glossary_hits" not in rc.cached_context("q", "prod", compute)

    def test_disabled_via_config(self):
        compute = MagicMock(side_effect=lambda: _ctx())
        with patch.object(rc, "_config", return_value={**rc._DEFAULTS, "enabled": False}):
            rc.cached_context("q", "prod", compute)
            rc.cached_context("q", "prod", compute)
        assert compute.call_count == 2

    def test_missing_config_section_uses_defaults(self):
        with patch("Utilities.base_utils.get_config_val", side_effect=KeyError("cache")):
            assert rc._config() == rc._DEFAULTS

    def test_config_values_size_the_cache(self):
        cfg = {"enabled": True, "ttl_seconds": 60, "max_entries": 1}
        compute = MagicMock(side_effect=lambda: _ctx())
        with patch.object(rc, "_config", return_value=cfg):
            rc.cached_context("a", "p", compute)
            rc.cached_context("b", "p", compute)   # evicts "a"
            rc.cached_context("a", "p", compute)
        assert compute.call_count == 3

    def test_clear_during_compute_prevents_caching_stale_result(self):
        def compute():
            result = _ctx()
            rc.clear()  # a metadata write lands while retrieval is running
            return result

        slow = MagicMock(side_effect=compute)
        assert rc.cached_context("q", "prod", slow)["table_list"]["direct"]
        rc.cached_context("q", "prod", slow)
        assert slow.call_count == 2

    def test_generation_guard_in_ttlcache(self):
        cache = rc.TTLCache()
        gen = cache.generation
        cache.clear()
        cache.set("k", 1, generation=gen)
        assert cache.get("k") is None
        cache.set("k", 1, generation=cache.generation)
        assert cache.get("k") == 1

    def test_clear_forces_recompute(self):
        compute = MagicMock(side_effect=lambda: _ctx())
        rc.cached_context("q", "prod", compute)
        rc.clear()
        rc.cached_context("q", "prod", compute)
        assert compute.call_count == 2


class TestGetRelevantContext:

    def test_repeated_question_retrieves_once_and_callers_may_mutate(self):
        with patch.object(main, "SQLBuilderSupport") as builder:
            builder.return_value.getBuildComponents.side_effect = lambda q: _ctx()
            first = main.getRelevantContext("top customers", instance_name="prod")
            first["glossary_hits"] = [{"term_name": "AUM"}]   # what generateQuery does
            second = main.getRelevantContext("top customers", instance_name="prod")
        assert builder.return_value.getBuildComponents.call_count == 1
        assert "glossary_hits" not in second

    def test_search_schema_uses_the_cache(self):
        from backend import mcp_server

        with patch.object(main, "SQLBuilderSupport") as builder, \
             patch.object(main, "_get_business_context", return_value=[]):
            builder.return_value.getBuildComponents.side_effect = lambda q: _ctx()
            mcp_server.search_schema("top customers", instance="prod")
            mcp_server.search_schema("top customers", instance="prod")
        assert builder.return_value.getBuildComponents.call_count == 1


# ---------------------------------------------------------------------------
# Invalidation from write paths
# ---------------------------------------------------------------------------

def _prime():
    """Put one entry in the cache and return a function that says whether it is still there."""
    compute = MagicMock(side_effect=lambda: _ctx())
    rc.cached_context("q", "prod", compute)

    def still_cached():
        before = compute.call_count
        rc.cached_context("q", "prod", compute)
        return compute.call_count == before

    assert still_cached()
    return still_cached


@pytest.fixture()
def glossary_db(monkeypatch):
    import MetadataManager.GlossaryStore as GS
    from Utilities.base_utils import accessDB

    conn = sqlite3.connect(":memory:")
    db = accessDB.__new__(accessDB)
    db.connection = conn
    db.cursor = conn.cursor()
    db.create_table(GS._TABLE_SCHEMA)
    monkeypatch.setattr(GS, "_get_db", lambda: db)
    yield GS
    conn.close()


class TestInvalidation:

    def test_glossary_writes(self, glossary_db):
        still_cached = _prime()
        term_id = glossary_db.add_term({"term_name": "AUM"})
        assert not still_cached()

        still_cached = _prime()
        glossary_db.update_term(term_id, {"definition": "assets"})
        assert not still_cached()

        still_cached = _prime()
        glossary_db.delete_term(term_id)
        assert not still_cached()

    def test_glossary_index_and_unindex(self):
        import MetadataManager.GlossaryStore as GS

        still_cached = _prime()
        with patch("MetadataManager.MetadataStore.vdb.Chroma.addData"):
            GS.index_term({"term_id": "t1", "term_name": "AUM"}, MagicMock(), MagicMock())
        assert not still_cached()

        still_cached = _prime()
        GS.unindex_term("t1", MagicMock())
        assert not still_cached()

    def test_okf_import_and_delete(self, tmp_path):
        from MetadataManager import OKFStore

        (tmp_path / "orders.md").write_text(
            "---\ntype: Table\ntitle: Orders\ndescription: d\n---\nbody\n", encoding="utf-8")
        still_cached = _prime()
        with patch("MetadataManager.MetadataStore.vdb.Chroma.addData"), \
             patch.object(OKFStore, "_collection", return_value=None):
            OKFStore.import_bundle(str(tmp_path), MagicMock(), MagicMock(), instance_name="prod")
        assert not still_cached()

        still_cached = _prime()
        collection = MagicMock()
        collection.get.return_value = {"ids": ["a"], "metadatas": [{"bundle": "b"}]}
        with patch.object(OKFStore, "_collection", return_value=collection):
            assert OKFStore.delete_bundle(MagicMock(), "prod", "b") == 1
        assert not still_cached()

    def test_store_table(self):
        from backend.ingestion import store_table

        still_cached = _prime()
        vdb = MagicMock()
        with patch("Utilities.base_utils.accessDB"), \
             patch("MetadataManager.MetadataBuilder.importExisting.importData.importDD"), \
             patch("MetadataManager.MetadataStore.relationdb.kuzuDB._get_conn"), \
             patch("MetadataManager.MetadataStore.relationdb.kuzuDB._ensure_schema"), \
             patch("MetadataManager.MetadataStore.relationdb.kuzuDB._merge_node"):
            store_table("orders", "d", [{"name": "id"}], [], instance_name="prod", vdb=vdb)
        assert not still_cached()

    def test_relationship_writes(self):
        from MetadataManager.MetadataStore.ManageRelations import Relations

        still_cached = _prime()
        rel = Relations(strgType="networkx", instance_name="prod")
        with patch.object(rel, "__instGraphObj__"), patch.object(Relations, "_backend", MagicMock()):
            rel.addRelation([["a", "b", "a.id=b.a_id", "fk"]])
        assert not still_cached()

    def test_failed_relationship_write_still_invalidates(self):
        from MetadataManager.MetadataStore.ManageRelations import Relations

        still_cached = _prime()
        rel = Relations(strgType="networkx", instance_name="prod")
        backend = MagicMock()
        backend.addRelations.side_effect = RuntimeError("disk full")
        with patch.object(rel, "__instGraphObj__"), patch.object(Relations, "_backend", backend), \
             pytest.raises(RuntimeError):
            rel.addRelation([["a", "b", "k", "fk"]])
        assert not still_cached()


# ---------------------------------------------------------------------------
# Shared models and clients
# ---------------------------------------------------------------------------

_CFG = {
    "models_repo": {"path": "/models"},
    "indexing": {"model": "embed"},
    "scoring": {"crossencoder": "rerank"},
}


def _cfg(config_type, keys, get_all=False):
    return _CFG[keys[0]]


class TestSharedModels:

    def test_embedding_model_loads_once(self):
        import sentence_transformers

        with patch("Utilities.base_utils.get_config_val", side_effect=_cfg), \
             patch.object(sentence_transformers, "SentenceTransformer") as st:
            assert shared_models.get_embedding_model() is shared_models.get_embedding_model()
        st.assert_called_once_with("/models/embed")

    def test_embedding_model_retries_after_failure(self):
        import sentence_transformers

        with patch("Utilities.base_utils.get_config_val", side_effect=_cfg), \
             patch.object(sentence_transformers, "SentenceTransformer",
                          side_effect=[OSError("model missing"), MagicMock()]) as st:
            with pytest.raises(OSError):
                shared_models.get_embedding_model()
            assert shared_models.get_embedding_model() is not None
        assert st.call_count == 2

    def test_reranker_loads_once(self):
        import FlagEmbedding

        with patch("Utilities.base_utils.get_config_val", side_effect=_cfg), \
             patch.object(FlagEmbedding, "FlagReranker") as fr:
            assert shared_models.get_reranker() is shared_models.get_reranker()
        fr.assert_called_once_with("/models/rerank", use_fp16=True)

    def test_chroma_client_per_path_and_retry(self):
        with patch("MetadataManager.MetadataStore.vdb.Chroma.getclient",
                   side_effect=[ValueError("locked"), MagicMock(), MagicMock()]) as gc:
            with pytest.raises(ValueError):
                shared_models.get_chroma_client("/db1")
            first = shared_models.get_chroma_client("/db1")
            assert shared_models.get_chroma_client("/db1") is first
            assert shared_models.get_chroma_client("/db2") is not first
        assert gc.call_count == 3

    def test_get_business_context_reuses_model_and_client(self, glossary_db):
        import sentence_transformers

        cfg = {**_CFG, "vectordb": {"path": "/vdb"}}
        raw = {"ids": [[]], "metadatas": [[]], "distances": [[]]}
        with patch("Utilities.base_utils.get_config_val", side_effect=lambda t, k, a=False: cfg[k[0]]), \
             patch.object(sentence_transformers, "SentenceTransformer") as st, \
             patch("MetadataManager.MetadataStore.vdb.Chroma.getclient") as gc, \
             patch("MetadataManager.MetadataStore.vdb.Chroma.getData", return_value=raw):
            glossary_db.get_business_context("a")
            glossary_db.get_business_context("b")
        assert st.call_count == 1
        assert gc.call_count == 1

    def test_get_business_context_degrades_then_recovers(self, glossary_db):
        import sentence_transformers

        cfg = {**_CFG, "vectordb": {"path": "/vdb"}}
        raw = {"ids": [[]], "metadatas": [[]], "distances": [[]]}
        with patch("Utilities.base_utils.get_config_val", side_effect=lambda t, k, a=False: cfg[k[0]]), \
             patch.object(sentence_transformers, "SentenceTransformer",
                          side_effect=[OSError("no model"), MagicMock()]), \
             patch("MetadataManager.MetadataStore.vdb.Chroma.getclient"), \
             patch("MetadataManager.MetadataStore.vdb.Chroma.getData", return_value=raw):
            assert glossary_db.get_business_context("a") == []   # failure -> []
            assert glossary_db.get_business_context("a") == []   # retried, loads fine, no hits
        assert shared_models.get_embedding_model.cache_info().currsize == 1
