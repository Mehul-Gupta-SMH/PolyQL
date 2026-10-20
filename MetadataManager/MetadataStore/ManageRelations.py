from .relationdb import networkxDB, kuzuDB
from MetadataManager.retrieval_cache import clear as clear_retrieval_cache

_BACKENDS = {
    "kuzu":    kuzuDB,
    "networkx": networkxDB,
}

class Relations:
    """
    A class for managing relations using various storage types.

    attr:
        - strgType (str): Storage backend — "kuzu" (default) or "networkx".
        - GraphObj: Graph object for storing relations.
    """

    def __init__(self, strgType="kuzu", instance_name: str = "default"):
        """
        Initialize the Relations object.

        args:
            - strgType (str): Storage type. Supported: "kuzu" (default), "networkx".
            - instance_name (str): Named database instance (e.g. "default", "snowflake_prod").
        """
        if strgType not in _BACKENDS:
            raise ValueError(f"Unknown strgType {strgType!r}. Choose from: {sorted(_BACKENDS)}")
        self.strgType = strgType
        self.instance_name = instance_name
        self.GraphObj = None

    @property
    def _backend(self):
        return _BACKENDS[self.strgType]

    def __instGraphObj__(self):
        self.GraphObj = self._backend.getObj(self.instance_name)

    def addRelation(self, edges, instance_name: str = None):
        """
        Add relations to the graph object.

        args:
            - edges (dict): Dictionary containing edge tuples as keys (source, target) and edge attributes as values.
            - instance_name (str): Override instance name for this call. Falls back to self.instance_name.
        """
        self.__instGraphObj__()
        iname = instance_name if instance_name is not None else self.instance_name
        try:
            self._backend.addRelations(self.GraphObj, edges, iname)
        finally:
            clear_retrieval_cache()  # join keys feed the cached schema context

    def getRelation(self, target_nodes=[], instance_name: str = None, unique_columns: set = None):
        """
        Retrieve relations from the graph object.

        args:
            - target_nodes (list): List of target nodes to retrieve relations for.
            - instance_name (str): Ignored (instance already set at construction). Kept for API symmetry.
            - unique_columns (set): {(table, column)} PRIMARY KEY / UNIQUE columns, used to
              rank joins by cardinality (kuzu backend only).

        returns:
            - dict: Dictionary containing retrieved relations.
        """
        self.__instGraphObj__()
        if unique_columns is not None and self.strgType == "kuzu":
            return self._backend.getRelations(self.GraphObj, target_nodes, unique_columns=unique_columns)
        return self._backend.getRelations(self.GraphObj, target_nodes)

    def visRelations(self):
        self.__instGraphObj__()
        self._backend.visualizeRelations(self.GraphObj)
        return "Refreshed relations map"
