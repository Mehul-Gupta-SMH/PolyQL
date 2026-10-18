"""
Utility functions for handling graph relations and attributes using NetworkX.

This module provides functions to load, add relations, and retrieve relations from a NetworkX graph.
"""

import networkx as nx
import pickle
import os
from pyvis.network import Network
from itertools import combinations
from iteration_utilities import unique_everseen
from Utilities.base_utils import get_config_val


# --------------------------------------------------------------------------------------------

def getObj(instance_name: str = "default"):
    """
    Load a NetworkX graph object for the specified instance from a pickle file.

    The pickle file may contain either:
      - A plain nx.DiGraph (old format, pre-multi-instance) — treated as "default"
      - A dict mapping instance_name -> nx.DiGraph (new multi-instance format)

    Returns:
        nx.DiGraph: Graph for the requested instance. Empty DiGraph if not found.
    """

    Graphfilename = get_config_val("retrieval_config", ["relationdb","path"])

    if os.path.exists(Graphfilename):
        with open(Graphfilename, "rb") as GObj:
            data = pickle.load(GObj)
        # Migrate old plain-graph format to dict format
        if isinstance(data, nx.Graph):
            graph_dict = {"default": data}
        else:
            graph_dict = data
    else:
        graph_dict = {}

    return graph_dict.get(instance_name, nx.DiGraph())

# --------------------------------------------------------------------------------------------

def addRelations(GObj: nx.DiGraph, edges: dict, instance_name: str = "default"):
    """
    Add nodes and edges with attributes to the per-instance NetworkX graph and save it.

    Loads the full graph dict, updates the sub-graph for *instance_name*, then
    persists the entire dict back to the pickle file.

    Args:
        GObj (nx.DiGraph): NetworkX graph object to which nodes and edges will be added.
        edges (dict): Dictionary containing edge tuples as keys (source, target) and edge attributes as values.
        instance_name (str): Instance to update. Defaults to "default".
    """

    # Add edges in both directions — JOINs are semantically bidirectional,
    # and DiGraph requires explicit reverse edges for path traversal.
    for edge in edges:
        GObj.add_edge(edge[0].lower(), edge[1].lower(), JoinKeys=edge[2])
        GObj.add_edge(edge[1].lower(), edge[0].lower(), JoinKeys=edge[2])

    Graphfilename = get_config_val("retrieval_config", ["relationdb", "path"])

    # Load full dict (or migrate old format), update instance, save back
    if os.path.exists(Graphfilename):
        with open(Graphfilename, "rb") as f:
            existing = pickle.load(f)
        if isinstance(existing, nx.Graph):
            graph_dict = {"default": existing}
        else:
            graph_dict = existing
    else:
        graph_dict = {}

    graph_dict[instance_name] = GObj

    with open(Graphfilename, 'wb') as f:
        pickle.dump(graph_dict, f)

# --------------------------------------------------------------------------------------------

def getRelations(GObj: nx.DiGraph, target_nodes: list):
    """
    Retrieve relations between target nodes in a graph.

    Args:
        GObj (nx.DiGraph): NetworkX graph object.
        target_nodes (list): List of target nodes.

    Returns:
        list: List of relations between target nodes along the shortest path.

    Notes:
        - This function finds the shortest path visiting all target nodes exactly once.
        - Relations are extracted along the shortest path, including edge attributes and node attributes.
    """
    # Initialize variables to store relations and shortest path
    relations = []
    shortest_path = dict()

    # Generate all possible paired combinations of target nodes
    combs = combinations(target_nodes, 2)

    # Find shortest path visiting all target nodes exactly once
    for comb in combs:

        # Skip pairs with no connecting path; keep resolving the remaining pairs
        if (comb[0].lower() not in GObj or comb[1].lower() not in GObj
                or not nx.has_path(GObj, comb[0].lower(), comb[1].lower())):
            continue

        # Compute the shortest path length between consecutive target nodes
        path = nx.shortest_path(GObj, source=comb[0].lower(), target=comb[1].lower())


        # Extract relations and attributes along the shortest path
        for i in range(len(path) - 1):
            relations.append({
                'source': path[i],
                'target': path[i + 1],
                'edge_attributes': GObj.get_edge_data(path[i], path[i + 1]),
                'node1_attributes': GObj.nodes[path[i]],
                'node2_attributes': GObj.nodes[path[i + 1]]
            })

        # Unique relations while maintaining their order
        relations = list(unique_everseen(relations))

    return relations
# --------------------------------------------------------------------------------------------

def visualizeRelations(GObj: nx.DiGraph):
    """
    Visualizes the relations in the graph and saves the graph as an HTML file.

    Args:
        GObj (nx.DiGraph): NetworkX graph object containing the relations.

    Returns:
        str: Message indicating that the HTML graph has been exported.
    """
    GraphViz = get_config_val("retrieval_config", ["relationdb", "viz"])
    # Initialize a Network object
    net = Network(height="1000px", width="100%")

    # Load the graph data into the Network object
    net.from_nx(GObj)

    # Save the graph as an HTML file
    net.save_graph(GraphViz)

    return "Html Graph : Exported"

