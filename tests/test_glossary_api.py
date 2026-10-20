"""
Glossary REST API (backend/glossary.py): auth, writes keep the
'business_glossary' embeddings in step, bulk upsert by name, rename clashes,
reindex. SQLite is in-memory; embedding is faked.
"""

import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)  # endpoints run in a worker thread
    from Utilities.base_utils import accessDB

    db = accessDB.__new__(accessDB)
    db.connection = conn
    db.cursor = conn.cursor()

    import MetadataManager.GlossaryStore as GS

    db.create_table(GS._TABLE_SCHEMA)
    monkeypatch.setattr(GS, "_get_db", lambda: db)
    yield db
    conn.close()


@pytest.fixture()
def embed(monkeypatch):
    """Records index/unindex calls; set embed.fail = True to simulate a missing model."""
    import MetadataManager.GlossaryStore as GS
    from backend import glossary

    state = MagicMock(fail=False, indexed=[], removed=[])
    rag = MagicMock()

    def fake_rag():
        if state.fail:
            raise RuntimeError("model not found")
        return rag

    monkeypatch.setattr(glossary, "_rag", fake_rag)
    monkeypatch.setattr(GS, "index_term", lambda term, model, client: state.indexed.append(term["term_name"]))
    monkeypatch.setattr(GS, "unindex_term", lambda term_id, client: state.removed.append(term_id))
    return state


@pytest.fixture()
def client():
    from backend.app import app

    return TestClient(app)


def _term(name="Net Revenue", **extra):
    return {"term_name": name, "definition": "Revenue after refunds", "formula": "SUM(o.total)",
            "table_deps": ["orders"], "synonyms": ["net sales"], "instance_name": "prod", **extra}


def test_requires_auth(client):
    from backend.app import app
    from backend.auth import get_current_user

    override = app.dependency_overrides.pop(get_current_user)
    try:
        codes = [client.get("/api/glossary/terms").status_code,
                 client.post("/api/glossary/terms/single", json=_term()).status_code,
                 client.post("/api/glossary/reindex").status_code]
    finally:
        app.dependency_overrides[get_current_user] = override
    assert codes == [401, 401, 401]


def test_create_single_embeds_the_term(client, embed):
    resp = client.post("/api/glossary/terms/single", json=_term())
    assert resp.status_code == 201
    assert resp.json()["indexed"] is True
    assert embed.indexed == ["Net Revenue"]

    term = client.get(f"/api/glossary/terms/{resp.json()['term_id']}").json()
    assert term["table_deps"] == ["orders"] and term["synonyms"] == ["net sales"]


def test_create_single_rejects_duplicate_name_in_same_instance(client, embed):
    client.post("/api/glossary/terms/single", json=_term())
    assert client.post("/api/glossary/terms/single", json=_term("net revenue")).status_code == 409
    # same name in another instance is a different term
    assert client.post("/api/glossary/terms/single", json=_term(instance_name="dev")).status_code == 201


def test_term_is_saved_when_embedding_fails(client, embed):
    embed.fail = True
    resp = client.post("/api/glossary/terms/single", json=_term())
    assert resp.status_code == 201 and resp.json()["indexed"] is False
    assert len(client.get("/api/glossary/terms").json()["terms"]) == 1


def test_generic_create_accepts_object_and_list(client, embed):
    one = client.post("/api/glossary/terms", json=_term("A"))
    many = client.post("/api/glossary/terms", json=[_term("B"), _term("C")])
    assert one.status_code == many.status_code == 201
    assert len(one.json()["created"]) == 1 and len(many.json()["created"]) == 2
    assert embed.indexed == ["A", "B", "C"]


def test_bulk_upsert_updates_by_name(client, embed):
    first = client.post("/api/glossary/terms/bulk?upsert=true", json=[_term("AUM"), _term("Churn")]).json()
    again = client.post("/api/glossary/terms/bulk?upsert=true",
                        json=[_term("aum", definition="Assets under management"), _term("ARPU")]).json()

    assert len(first["created"]) == 2
    assert again["updated"] == [first["created"][0]] and len(again["created"]) == 1
    terms = {t["term_name"]: t for t in client.get("/api/glossary/terms?instance_name=prod").json()["terms"]}
    assert set(terms) == {"AUM", "Churn", "ARPU"}  # name kept, no duplicate
    assert terms["AUM"]["definition"] == "Assets under management"


def test_bulk_without_upsert_duplicates(client, embed):
    client.post("/api/glossary/terms/bulk", json=[_term("AUM")])
    client.post("/api/glossary/terms/bulk", json=[_term("AUM")])
    assert len(client.get("/api/glossary/terms").json()["terms"]) == 2


def test_bulk_with_blank_name_writes_nothing(client, embed):
    resp = client.post("/api/glossary/terms/bulk", json=[_term("AUM"), _term("  ")])
    assert resp.status_code == 422 and "Term 2" in resp.json()["detail"]
    assert client.get("/api/glossary/terms").json()["terms"] == []


def test_update_reembeds_and_checks_rename(client, embed):
    a = client.post("/api/glossary/terms/single", json=_term("AUM")).json()["term_id"]
    client.post("/api/glossary/terms/single", json=_term("Churn"))
    embed.indexed.clear()

    ok = client.put(f"/api/glossary/terms/{a}", json={"definition": "Assets", "term_name": "AUM"})
    assert ok.status_code == 200 and ok.json()["indexed"] is True and embed.indexed == ["AUM"]
    assert client.put(f"/api/glossary/terms/{a}", json={"term_name": "churn"}).status_code == 409
    assert client.put(f"/api/glossary/terms/{a}", json={"term_name": " "}).status_code == 422
    assert client.put("/api/glossary/terms/missing", json={"definition": "x"}).status_code == 404


def test_delete_removes_embedding(client, embed):
    tid = client.post("/api/glossary/terms/single", json=_term()).json()["term_id"]
    assert client.delete(f"/api/glossary/terms/{tid}").json() == {"ok": True}
    assert embed.removed == [tid]
    assert client.get(f"/api/glossary/terms/{tid}").status_code == 404


def test_delete_succeeds_when_vector_store_unavailable(client, embed):
    tid = client.post("/api/glossary/terms/single", json=_term()).json()["term_id"]
    embed.fail = True
    assert client.delete(f"/api/glossary/terms/{tid}").status_code == 200


def test_reindex(client, embed):
    client.post("/api/glossary/terms/bulk", json=[_term("A"), _term("B", instance_name="dev")])
    embed.indexed.clear()
    assert client.post("/api/glossary/reindex?instance_name=dev").json() == {"indexed": 1}
    assert embed.indexed == ["B"]
    embed.fail = True
    assert client.post("/api/glossary/reindex").status_code == 503


def test_unindex_term_deletes_by_chroma_id():
    import MetadataManager.GlossaryStore as GS

    chroma = MagicMock()
    GS.unindex_term("t1", chroma)
    chroma.get_collection.return_value.delete.assert_called_once_with(ids=[GS._chroma_id("t1")])

    chroma.get_collection.side_effect = ValueError("no collection")
    GS.unindex_term("t1", chroma)  # no error
