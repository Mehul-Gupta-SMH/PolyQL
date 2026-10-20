"""
Join-path planning over the relations graph.

The Kuzu graph holds two kinds of edges:
  fk       — real join relationships with join keys ("orders.customer_id = customers.id")
  lineage  — pipeline edges (source table → derived table), usually without keys
Only fk edges with join keys are used to plan joins; lineage edges stay in the
graph for the lineage / derivatives views.

Planning:
  1. Every usable edge gets a cardinality from PRIMARY KEY / UNIQUE columns
     (1:1, 1:n, n:1, n:m) and a weight — n:m hops cost more, so paths prefer
     key-to-key joins that don't multiply rows.
  2. All requested tables are connected with ONE minimal tree (approximate
     Steiner tree, networkx) instead of merging pairwise shortest paths, which
     could add redundant or circular joins once 3+ tables are involved.
  3. When two tables can be joined several ways (role-playing dimensions:
     order_date_id / ship_date_id → dim_date) every alternative is returned.
"""

import logging
import re

import networkx as nx

logger = logging.getLogger(__name__)

LINEAGE = "lineage"
FK = "fk"

# Edge weights: prefer joins that follow a key; avoid many-to-many
_WEIGHTS = {"1:1": 1.0, "n:1": 1.0, "1:n": 1.0, "n:m": 3.0}

def _side(expr: str) -> tuple:
    """'schema.table.col' → ('schema.table', 'col'); quotes removed, lowercased."""
    name = re.sub(r'[`"\[\]\s]', "", expr).lower()
    table, _, column = name.rpartition(".")
    return table, column


def join_type(join_keys: str, src: str, tgt: str, unique_columns: set) -> str:
    """
    Cardinality of joining *src* to *tgt* on *join_keys*.

    A side is "1" when the key column on that side is PRIMARY KEY / UNIQUE.
    Returns "1:1", "1:n" (each src row matches many tgt rows), "n:1" or "n:m".
    """
    src, tgt = src.lower(), tgt.lower()
    src_unique = tgt_unique = False
    for expr in (part for part in (join_keys or "").split(",") if "=" in part):
        left, right = expr.split("=", 1)
        (lt, lc), (rt, rc) = _side(left), _side(right)
        if lt == src and rt == tgt:
            src_unique |= (src, lc) in unique_columns
            tgt_unique |= (tgt, rc) in unique_columns
        elif lt == tgt and rt == src:
            src_unique |= (src, rc) in unique_columns
            tgt_unique |= (tgt, lc) in unique_columns
    if src_unique and tgt_unique:
        return "1:1"
    if src_unique:
        return "1:n"
    if tgt_unique:
        return "n:1"
    return "n:m"


def load_unique_columns(instance_name: str = "default") -> set:
    """{(table_lower, column_lower)} for PRIMARY KEY / UNIQUE columns; empty set on any error."""
    try:
        from Utilities.base_utils import accessDB, get_config_val

        tmddb = get_config_val("retrieval_config", ["tableMDdb"], True)
        db = accessDB(tmddb["info_type"], tmddb["dbName"])
        inst_filter = {} if instance_name == "default" else {"instance_name": instance_name}
        rows = db.get_data(tmddb["tableColName"], inst_filter,
                           ["TableName", "ColumnName", "Constraints"], fetchtype="All") or []
    except Exception as exc:
        logger.warning("joinpath: could not load key columns — %s", exc)
        return set()
    unique = set()
    for table, column, constraints in rows:
        text = (constraints or "").upper()
        if table and column and ("PRIMARY KEY" in text or "UNIQUE" in text):
            unique.add((table.lower(), column.lower()))
    return unique


def _options(data: dict) -> list:
    """Every join condition stored on an edge (alternatives first, else JoinKeys)."""
    options = data.get("JoinKeysAlternatives") or [data.get("JoinKeys", "")]
    return [o for o in options if o]


def is_join_edge(data: dict) -> bool:
    """True for edges usable in joins: not lineage, and with at least one join condition."""
    return data.get("Kind", FK) != LINEAGE and bool(_options(data))


def join_graph(G: nx.DiGraph, unique_columns: set) -> nx.Graph:
    """
    Undirected, weighted graph of the usable join edges in *G*.

    Edge attributes: weight (cheapest option), options [join_keys, ...].
    """
    J = nx.Graph()
    J.add_nodes_from(G.nodes())
    for src, tgt, data in G.edges(data=True):
        if not is_join_edge(data):
            continue
        options = _options(data)
        if J.has_edge(src, tgt):
            known = J[src][tgt]["options"]
            options = known + [o for o in options if o not in known]
        weight = min(_WEIGHTS[join_type(o, src, tgt, unique_columns)] for o in options)
        J.add_edge(src, tgt, weight=weight, options=options)
    return J


def connect_tables(G: nx.DiGraph, tables: list, unique_columns: set | None = None) -> tuple:
    """
    Plan the joins that connect *tables*.

    Returns (relations, unreachable):
      relations   [{source, target, join_keys, cardinality, alternatives}] in
                  traversal order; alternatives lists every join condition for
                  that hop (join_keys is the first).
      unreachable tables that can't be joined to any other requested table.
    """
    unique_columns = unique_columns or set()
    J = join_graph(G, unique_columns)
    terminals = [t for t in dict.fromkeys(t.lower() for t in tables) if t in J]

    relations, unreachable = [], []
    for component in nx.connected_components(J):
        group = [t for t in terminals if t in component]
        if len(group) < 2:
            unreachable += group
            continue
        tree = nx.algorithms.approximation.steiner_tree(J.subgraph(component), group, weight="weight")
        for parent, child in nx.bfs_edges(tree, group[0]):
            options = J[parent][child]["options"]
            relations.append({
                "source": parent,
                "target": child,
                "join_keys": options[0],
                "cardinality": join_type(options[0], parent, child, unique_columns),
                "alternatives": options,
            })
    if len(terminals) < 2:
        unreachable = []
    return relations, unreachable
