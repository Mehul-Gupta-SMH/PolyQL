import networkx as nx

from MetadataManager.MetadataStore.relationdb import kuzuDB


def _prepare_temp_relations(monkeypatch, tmp_path):
    """Point kuzuDB at a temporary Relations.pickle path under tmp_path."""
    relations_dir = tmp_path / "relationsdb"
    relations_dir.mkdir(parents=True, exist_ok=True)
    (relations_dir / "kuzudb").mkdir(exist_ok=True)
    pickle_path = relations_dir / "Relations.pickle"
    monkeypatch.setattr(kuzuDB, "get_config_val", lambda *_: str(pickle_path))
    return pickle_path


def test_getObj_returns_nx_graph(tmp_path, monkeypatch):
    _prepare_temp_relations(monkeypatch, tmp_path)
    graph = kuzuDB.getObj("sample-instance")

    assert isinstance(graph, nx.DiGraph)
    assert graph.number_of_nodes() == 0


def test_addRelations_persists_and_updates_GObj(tmp_path, monkeypatch):
    _prepare_temp_relations(monkeypatch, tmp_path)
    graph = nx.DiGraph()
    edges = [("Orders", "Customers", "orders.customer_id = customers.customer_id")]

    kuzuDB.addRelations(graph, edges, instance_name="commerce")

    assert graph.has_edge("orders", "customers")
    persisted = kuzuDB.getObj("commerce")
    assert persisted.has_edge("orders", "customers")
    assert persisted["orders"]["customers"]["JoinKeys"] == edges[0][2]


def test_getRelations_returns_shortest_path_edges(tmp_path, monkeypatch):
    _prepare_temp_relations(monkeypatch, tmp_path)
    graph = kuzuDB.getObj("path-instance")
    kuzuDB.addRelations(
        graph,
        [
            ("Source", "Bridge", "source.bridge_id = bridge.id"),
            ("Bridge", "Target", "bridge.target_id = target.id"),
        ],
        instance_name="path-instance",
    )

    graph = kuzuDB.getObj("path-instance")
    relations = kuzuDB.getRelations(graph, ["Source", "Target"])
    expected = []
    path = nx.shortest_path(graph, source="source", target="target")
    for i in range(len(path) - 1):
        expected.append({
            "source": path[i],
            "target": path[i + 1],
            "edge_attributes": graph.get_edge_data(path[i], path[i + 1]),
            "node1_attributes": graph.nodes[path[i]],
            "node2_attributes": graph.nodes[path[i + 1]],
        })
    assert relations == expected

    assert len(relations) == 2
    assert relations[0]["source"] == "source"
    assert relations[-1]["target"] == "target"
    assert relations[-1]["edge_attributes"]["JoinKeys"] == "bridge.target_id = target.id"


def test_getObj_attempts_pickle_migration(tmp_path, monkeypatch):
    pickle_path = _prepare_temp_relations(monkeypatch, tmp_path)
    pickle_path.write_bytes(b"")  # existence triggers migration

    # Bypass lru_cache on _kuzu_base_dir so this test's tmp_path is used
    # regardless of what earlier tests may have cached.
    kuzu_base = str(tmp_path / "relationsdb" / "kuzudb")
    monkeypatch.setattr(kuzuDB, "_kuzu_base_dir", lambda: kuzu_base)

    # Clear any module-level pool/schema state for this instance name so the
    # DB is treated as fresh (is_fresh=True) and migration logic is triggered.
    kuzuDB._DB_POOL.pop("legacy-instance", None)
    kuzuDB._SCHEMA_READY.discard("legacy-instance")

    captured = {"called": False}

    def fake_load(_fh):
        captured["called"] = True
        dummy = nx.DiGraph()
        dummy.add_edge("Legacy", "Db", JoinKeys="legacy.relation = db.id")
        return dummy

    monkeypatch.setattr(kuzuDB.pickle, "load", fake_load)

    graph = kuzuDB.getObj("legacy-instance")

    assert captured["called"] is True
    assert graph.has_edge("legacy", "db")
    assert graph["legacy"]["db"]["JoinKeys"] == "legacy.relation = db.id"


def test_getRelations_skips_unreachable_pair_and_resolves_rest(tmp_path, monkeypatch):
    _prepare_temp_relations(monkeypatch, tmp_path)
    graph = kuzuDB.getObj("disconnected-instance")
    kuzuDB.addRelations(
        graph,
        [
            ("Orders", "Customers", "orders.customer_id = customers.id"),
            ("Customers", "Regions", "customers.region_id = regions.id"),
        ],
        instance_name="disconnected-instance",
    )
    kuzuDB._merge_node(kuzuDB._get_conn("disconnected-instance"), "audit_log")

    graph = kuzuDB.getObj("disconnected-instance")
    # The first pair (orders, audit_log) has no path; later pairs must still resolve.
    relations = kuzuDB.getRelations(graph, ["Orders", "Audit_Log", "Regions", "Unknown_Table"])

    hops = {(r["source"], r["target"]) for r in relations}
    assert hops == {("orders", "customers"), ("customers", "regions")}


def test_role_playing_join_keys_are_preserved(tmp_path, monkeypatch):
    _prepare_temp_relations(monkeypatch, tmp_path)
    order_date = "orders.order_date_id = dim_date.date_id"
    ship_date = "orders.ship_date_id = dim_date.date_id"
    graph = kuzuDB.getObj("role-instance")
    kuzuDB.addRelations(
        graph,
        [("Orders", "Dim_Date", order_date), ("Orders", "Dim_Date", ship_date)],
        instance_name="role-instance",
    )

    # In-place update and reload from Kuzu both keep both conditions
    for g in (graph, kuzuDB.getObj("role-instance")):
        assert g["orders"]["dim_date"]["JoinKeysAlternatives"] == [order_date, ship_date]
        assert g["orders"]["dim_date"]["JoinKeys"] == order_date

    relations = kuzuDB.getRelations(kuzuDB.getObj("role-instance"), ["Orders", "Dim_Date"])
    assert [r["edge_attributes"]["JoinKeys"] for r in relations] == [order_date, ship_date]


def test_empty_join_keys_dropped_when_real_condition_exists():
    graph = nx.DiGraph()
    kuzuDB._add_graph_edge(graph, "a", "b", "")
    kuzuDB._add_graph_edge(graph, "a", "b", "a.id = b.a_id")

    assert graph["a"]["b"]["JoinKeys"] == "a.id = b.a_id"
    assert "JoinKeysAlternatives" not in graph["a"]["b"]
