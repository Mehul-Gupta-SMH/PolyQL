"""
Kuzu-backed graph store — drop-in replacement for networkxDB.py.

Public API is identical:
    getObj(instance_name)        -> nx.DiGraph
    addRelations(GObj, edges, instance_name)
    getRelations(GObj, target_nodes) -> list
    visualizeRelations(GObj)

Kuzu is an embedded graph database (no server required).  Each named
instance gets its own Kuzu database directory:
    <relationsdb_dir>/kuzudb/<instance_name>/

On first access of an instance, if a legacy Relations.pickle exists the
data is automatically migrated into Kuzu and the pickle is left in place
(read-only, as a backup).
"""

import logging
import os
import pickle
from functools import lru_cache

import kuzu
import networkx as nx
from itertools import combinations

from Utilities.base_utils import get_config_val

logger = logging.getLogger(__name__)

_NODE_TABLE = "KTable"
_REL_TABLE  = "JoinRel"

# Per-instance DB pool — kuzu.Database is heavyweight (opens file handles),
# so we create it once and reuse the object for the process lifetime.
_DB_POOL: dict = {}
# Track instances whose schema has already been ensured this session.
_SCHEMA_READY: set = set()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _kuzu_base_dir() -> str:
    """Directory that holds one Kuzu DB sub-folder per instance. Cached after first call."""
    pickle_path = get_config_val("retrieval_config", ["relationdb", "path"])
    return os.path.join(os.path.dirname(pickle_path), "kuzudb")


def _instance_db_path(instance_name: str) -> str:
    return os.path.join(_kuzu_base_dir(), instance_name)


def _get_conn(instance_name: str) -> kuzu.Connection:
    """Return a connection for *instance_name*, reusing a pooled kuzu.Database."""
    if instance_name not in _DB_POOL:
        db_path = _instance_db_path(instance_name)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        _DB_POOL[instance_name] = kuzu.Database(db_path)
    return kuzu.Connection(_DB_POOL[instance_name])


def _ensure_schema(conn: kuzu.Connection, instance_name: str) -> None:
    """Create node/rel tables if not already done for this instance this session."""
    if instance_name in _SCHEMA_READY:
        return
    conn.execute(
        f"CREATE NODE TABLE IF NOT EXISTS {_NODE_TABLE}"
        f"(name STRING, PRIMARY KEY(name))"
    )
    conn.execute(
        f"CREATE REL TABLE IF NOT EXISTS {_REL_TABLE}"
        f"(FROM {_NODE_TABLE} TO {_NODE_TABLE}, JoinKeys STRING)"
    )
    _SCHEMA_READY.add(instance_name)


def _open_instance(instance_name: str) -> tuple:
    """
    Ensure the Kuzu DB exists, schema is ready, and migration has run.
    Returns (conn, is_fresh) where is_fresh=True means the DB was just created.
    """
    db_path = _instance_db_path(instance_name)
    is_fresh = not os.path.exists(db_path)
    conn = _get_conn(instance_name)
    _ensure_schema(conn, instance_name)
    if is_fresh:
        _migrate_from_pickle(instance_name, conn)
    return conn, is_fresh


def _merge_node(conn: kuzu.Connection, name: str) -> None:
    conn.execute(f"MERGE (t:{_NODE_TABLE} {{name: $n}})", {"n": name})


def _upsert_edge(conn: kuzu.Connection, src: str, tgt: str, join_keys) -> None:
    """Insert edge if it doesn't exist using a single MERGE round-trip."""
    conn.execute(
        f"MATCH (a:{_NODE_TABLE} {{name: $s}}), (b:{_NODE_TABLE} {{name: $t}})"
        f" MERGE (a)-[:{_REL_TABLE} {{JoinKeys: $jk}}]->(b)",
        {"s": src, "t": tgt, "jk": str(join_keys) if join_keys else ""},
    )


def _add_graph_edge(G: nx.DiGraph, src: str, tgt: str, join_keys) -> None:
    """
    Add an edge to *G* without losing alternative join conditions.

    nx.DiGraph holds one edge per (src, tgt), but the same pair can be joined
    in more than one way (e.g. orders.order_date_id / orders.ship_date_id
    → dim_date). ``JoinKeys`` keeps the first condition for existing callers;
    ``JoinKeysAlternatives`` is set only when there is more than one.
    Empty join keys are dropped once a real condition is known.
    """
    join_keys = str(join_keys) if join_keys else ""
    if not G.has_edge(src, tgt):
        G.add_edge(src, tgt, JoinKeys=join_keys)
        return

    data = G[src][tgt]
    options = list(data.get("JoinKeysAlternatives") or [data.get("JoinKeys", "")])
    if join_keys in options:
        return
    options = [jk for jk in options + [join_keys] if jk] or [""]
    data["JoinKeys"] = options[0]
    if len(options) > 1:
        data["JoinKeysAlternatives"] = options
    else:
        data.pop("JoinKeysAlternatives", None)


def _migrate_from_pickle(instance_name: str, conn: kuzu.Connection) -> None:
    """
    Import all nodes/edges from a legacy Relations.pickle into Kuzu.
    Called exactly once — when the Kuzu DB directory is first created.
    """
    try:
        pickle_path = get_config_val("retrieval_config", ["relationdb", "path"])
        if not os.path.exists(pickle_path):
            return

        with open(pickle_path, "rb") as f:
            data = pickle.load(f)

        graph: nx.DiGraph = (
            data if isinstance(data, nx.Graph)
            else data.get(instance_name, nx.DiGraph())
        )

        for node in graph.nodes():
            _merge_node(conn, node.lower())

        for src, tgt, edge_data in graph.edges(data=True):
            _upsert_edge(conn, src.lower(), tgt.lower(), edge_data.get("JoinKeys", ""))

        logger.info(
            "Migrated Relations.pickle → Kuzu for instance '%s': "
            "%d nodes, %d edges",
            instance_name, graph.number_of_nodes(), graph.number_of_edges(),
        )
    except Exception as exc:
        logger.warning(
            "Pickle → Kuzu migration failed for instance '%s': %s", instance_name, exc
        )


# ---------------------------------------------------------------------------
# Public API  (mirrors networkxDB.py)
# ---------------------------------------------------------------------------

def getObj(instance_name: str = "default") -> nx.DiGraph:
    """
    Load the graph for *instance_name* from Kuzu and return it as nx.DiGraph.

    Automatically migrates from a legacy Relations.pickle on first call.
    """
    conn, _ = _open_instance(instance_name)

    G = nx.DiGraph()

    res = conn.execute(f"MATCH (t:{_NODE_TABLE}) RETURN t.name")
    while res.has_next():
        G.add_node(res.get_next()[0])

    res = conn.execute(
        f"MATCH (a:{_NODE_TABLE})-[r:{_REL_TABLE}]->(b:{_NODE_TABLE})"
        f" RETURN a.name, r.JoinKeys, b.name"
        f" ORDER BY a.name, b.name, r.JoinKeys"
    )
    while res.has_next():
        row = res.get_next()
        _add_graph_edge(G, row[0], row[2], row[1])

    return G


def addRelations(GObj: nx.DiGraph, edges: dict, instance_name: str = "default") -> None:
    """
    Persist *edges* to the Kuzu DB for *instance_name* and update *GObj* in place.

    Each edge is stored in both directions (JOINs are semantically bidirectional).
    """
    conn, _ = _open_instance(instance_name)

    for edge in edges:
        src = edge[0].lower()
        tgt = edge[1].lower()
        join_keys = edge[2] if len(edge) > 2 else ""

        _merge_node(conn, src)
        _merge_node(conn, tgt)
        _upsert_edge(conn, src, tgt, join_keys)
        _upsert_edge(conn, tgt, src, join_keys)  # bidirectional

        _add_graph_edge(GObj, src, tgt, join_keys)
        _add_graph_edge(GObj, tgt, src, join_keys)


def getRelations(GObj: nx.DiGraph, target_nodes: list) -> list:
    """
    Retrieve relations between *target_nodes* via shortest-path traversal.
    Operates on the in-memory *GObj* (already loaded from Kuzu by getObj).

    Pairs with no connecting path (or unknown tables) are skipped; the
    remaining pairs are still resolved. A hop with several alternative join
    conditions yields one relation per alternative so all of them reach the
    prompt.
    """
    relations = []

    for comb in combinations(target_nodes, 2):
        src, tgt = comb[0].lower(), comb[1].lower()
        if src not in GObj or tgt not in GObj or not nx.has_path(GObj, src, tgt):
            continue

        path = nx.shortest_path(GObj, source=src, target=tgt)

        for i in range(len(path) - 1):
            edge_data = GObj.get_edge_data(path[i], path[i + 1])
            alternatives = (edge_data or {}).get("JoinKeysAlternatives") or [None]
            for join_keys in alternatives:
                attrs = edge_data if join_keys is None else {**edge_data, "JoinKeys": join_keys}
                relations.append({
                    "source": path[i],
                    "target": path[i + 1],
                    "edge_attributes": attrs,
                    "node1_attributes": GObj.nodes[path[i]],
                    "node2_attributes": GObj.nodes[path[i + 1]],
                })

    deduped = []
    seen = set()
    for rel in relations:
        edge_attrs = rel["edge_attributes"] or {}
        join_keys = edge_attrs.get("JoinKeys")
        key = (rel["source"], rel["target"], join_keys)
        if key not in seen:
            seen.add(key)
            deduped.append(rel)

    return deduped


def visualizeRelations(GObj: nx.DiGraph) -> str:
    """Export graph to HTML via pyvis (unchanged from networkxDB)."""
    from pyvis.network import Network

    GraphViz = get_config_val("retrieval_config", ["relationdb", "viz"])
    net = Network(height="1000px", width="100%")
    net.from_nx(GObj)
    net.save_graph(GraphViz)
    return "Html Graph : Exported"
