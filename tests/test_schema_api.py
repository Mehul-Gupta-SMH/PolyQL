"""GET /api/schema — directed relations with cardinality, alternatives and kind."""
from unittest.mock import MagicMock, patch

import networkx as nx
import pytest
from fastapi.testclient import TestClient

from backend.app import app

UNIQUE = {("customers", "id"), ("orders", "id"), ("dim_date", "id"), ("profiles", "customer_id"),
          ("customers", "profile_id")}


@pytest.fixture
def client():
    return TestClient(app)


def _graph():
    G = nx.DiGraph()
    G.add_nodes_from(["orders", "customers", "dim_date", "profiles", "tags", "orders_daily"])
    # fk stored both ways, like addRelations does
    G.add_edge("orders", "customers", JoinKeys="orders.customer_id = customers.id", Kind="fk")
    G.add_edge("customers", "orders", JoinKeys="orders.customer_id = customers.id", Kind="fk")
    # role-playing dates: two conditions on one edge
    two = ["orders.order_date_id = dim_date.id", "orders.ship_date_id = dim_date.id"]
    G.add_edge("orders", "dim_date", JoinKeys=two[0], JoinKeysAlternatives=two, Kind="fk")
    G.add_edge("dim_date", "orders", JoinKeys=two[0], JoinKeysAlternatives=two, Kind="fk")
    # one-to-one and many-to-many
    G.add_edge("customers", "profiles", JoinKeys="customers.id = profiles.customer_id", Kind="fk")
    G.add_edge("orders", "tags", JoinKeys="orders.region = tags.region", Kind="fk")
    # lineage without keys
    G.add_edge("orders", "orders_daily", JoinKeys="", Kind="lineage")
    # a reverse edge with different conditions is a distinct relation
    G.add_edge("profiles", "customers", JoinKeys="profiles.customer_id = customers.id", Kind="fk")
    G.add_edge("customers", "profiles", JoinKeys="customers.id = profiles.customer_id", Kind="fk")
    return G


def _call(client, graph, unique=UNIQUE):
    db = MagicMock()
    db.get_data.side_effect = lambda table, lookup, cols, fetchtype="one": (
        ["a table"] if cols == ["Desc"] else [("id", "INT", "PRIMARY KEY", "key")])
    cfg = {"info_type": "sqlite", "dbName": "x", "tableDescName": "d", "tableColName": "c"}
    with patch("MetadataManager.MetadataStore.relationdb.kuzuDB.getObj", return_value=graph), \
         patch("MetadataManager.MetadataStore.relationdb.joinpath.load_unique_columns",
               return_value=unique) as load, \
         patch("Utilities.base_utils.get_config_val", return_value=cfg), \
         patch("Utilities.base_utils.accessDB", return_value=db):
        res = client.get("/api/schema?instance_name=prod")
    assert res.status_code == 200
    load.assert_called_once_with("prod")
    return res.json()


def _rel(body, src, tgt):
    return next(r for r in body["relations"] if (r["source"], r["target"]) == (src, tgt))


def test_tables_unchanged(client):
    body = _call(client, _graph())
    assert [t["name"] for t in body["tables"]] == sorted(_graph().nodes())
    assert body["tables"][0]["columns"][0] == {"name": "id", "type": "INT", "constraints": "PRIMARY KEY",
                                              "description": "key"}


def test_cardinality_per_edge(client):
    body = _call(client, _graph())
    assert _rel(body, "orders", "customers")["cardinality"] == "n:1"
    assert _rel(body, "orders", "dim_date")["cardinality"] == "n:1"
    assert _rel(body, "orders", "tags")["cardinality"] == "n:m"
    assert _rel(body, "customers", "profiles")["cardinality"] == "1:1"


def test_mirror_edge_drawn_once(client):
    body = _call(client, _graph())
    pairs = [(r["source"], r["target"]) for r in body["relations"]]
    assert ("customers", "orders") not in pairs
    assert ("dim_date", "orders") not in pairs
    assert len(pairs) == len(set(pairs))


def test_reverse_edge_with_different_conditions_is_kept(client):
    G = nx.DiGraph()
    G.add_edge("orders", "customers", JoinKeys="orders.customer_id = customers.id", Kind="fk")
    G.add_edge("customers", "orders", JoinKeys="orders.referrer_id = customers.id", Kind="fk")
    body = _call(client, G)
    assert _rel(body, "orders", "customers")["cardinality"] == "n:1"
    assert _rel(body, "customers", "orders")["cardinality"] == "1:n"
    assert _rel(body, "customers", "orders")["joinKeys"] == "orders.referrer_id = customers.id"


def test_alternatives_preserved(client):
    rel = _rel(_call(client, _graph()), "orders", "dim_date")
    assert rel["alternatives"] == ["orders.order_date_id = dim_date.id", "orders.ship_date_id = dim_date.id"]
    assert rel["joinKeys"] == rel["alternatives"][0]


def test_single_condition_edge(client):
    rel = _rel(_call(client, _graph()), "orders", "customers")
    assert rel["joinKeys"] == "orders.customer_id = customers.id"
    assert rel["alternatives"] == [rel["joinKeys"]]
    assert rel["kind"] == "fk"


def test_lineage_has_kind_and_no_cardinality(client):
    rel = _rel(_call(client, _graph()), "orders", "orders_daily")
    assert rel["kind"] == "lineage"
    assert rel["cardinality"] is None
    assert rel["joinKeys"] == "" and rel["alternatives"] == []


def test_unknown_keys_give_many_to_many(client):
    assert _rel(_call(client, _graph(), unique=set()), "orders", "customers")["cardinality"] == "n:m"
