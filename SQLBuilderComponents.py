"""
Module for providing support for SQL query building. This module contains classes and methods
to provide necessary components for building SQL queries.

Class:
    - SQLBuilderSupport: Class to support SQL query building by providing necessary components.
"""
import functools
import logging

from rank_bm25 import BM25Okapi

from Utilities.base_utils import get_config_val, accessDB
from MetadataManager.MetadataStore import RAGPipeline, ManageRelations

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=256)
def _build_bm25(corpus_key: tuple) -> BM25Okapi:
    """
    Build and cache a BM25Okapi index for a given column corpus.

    corpus_key is a tuple of token-tuples — one per column — derived from
    the column name and description. Using an immutable key makes the result
    cacheable with lru_cache, so the same table schema reuses the same index
    across queries instead of rebuilding it every time.
    """
    return BM25Okapi([list(tokens) for tokens in corpus_key])




def _truncate(text: str, limit: int) -> str:
    """Cut *text* to at most *limit* characters, on a word boundary, with an ellipsis."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(None, 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip() + " …"




class SQLBuilderSupport:
    """
    Class to support SQL query building by providing necessary components.

    Attributes:
        user_query (str): The user's SQL query.
        table_list (dict): A dictionary containing relevant tables, both direct and intermediate.
        join_keys (dict): A dictionary containing table relations and associated join keys.
        table_metadata (dict): A dictionary containing metadata information for tables.
    """
    def __init__(self, instance_name: str = "default"):
        """
        Initializes an instance of SQLBuilderSupport class.

        Args:
            instance_name (str): Named database instance to scope all metadata lookups.
                                 Defaults to "default" for backward compatibility.

        sample format of the table_list
        table_list:
            direct:
                tablename:
                    desc:
                    columns:
                        name:
                        dtype:
                        desc:
            intermediate:
        """
        self.user_query = None
        self.instance_name = instance_name
        self.table_list = {
            "direct" : {},
            "intermediate" : {}
        }
        self.join_keys = None
        self.table_metadata = {}
        self.okf_hits = []

        self.vdb_config = get_config_val("retrieval_config", ["vectordb"],True)
        self.tmddb_config = get_config_val("retrieval_config", ["tableMDdb"],True)

        self.DBObj = accessDB(self.tmddb_config['info_type'], self.tmddb_config['dbName'])

    def __filterRelevantResults__(self, results_w_scores):
        """
        Filter and rank retrieved tables by reranker score.

        Tables whose reranker score falls below the configured threshold are dropped.
        Remaining tables are ordered from most to least relevant.
        """
        threshold = get_config_val("retrieval_config", ["scoring", "reranker_threshold"])

        passed = {
            uid: vals for uid, vals in results_w_scores.items()
            if vals['scores']['reranker'] > threshold
        }

        logger.debug(
            "Table retrieval: %d returned, %d passed reranker threshold (%.2f)",
            len(results_w_scores), len(passed), threshold
        )

        sorted_results = sorted(
            passed.items(),
            key=lambda x: x[1]['scores']['reranker'],
            reverse=True
        )

        filtered_results_dict = {}
        for _, vals in sorted_results:
            filtered_results_dict[vals['metadata']['TableName']] = {
                "description": vals['data'],
                "columns": {}
            }

        return filtered_results_dict

    def __getRelevantTables__(self):
        """
        Retrieve relevant tables based on the user's query.

        Returns:
            list: A list of relevant tables.
        """

        # Initializing a ManageInformation object for data retrieval
        RetrievalObj = RAGPipeline.ManageInformation()

        # Initializing the client
        RetrievalObj.initialize_client()

        # Retrieving data based on the user query, filtered by instance when not "default"
        results_scored = RetrievalObj.get_data(
            self.user_query,
            self.vdb_config["metadata"],
            instance_name=self.instance_name if self.instance_name != "default" else None,
        )

        # Performing filtering on the retrieved results
        filtered_results = self.__filterRelevantResults__(results_scored)

        # Updating the 'direct' table list attribute with the filtered results
        self.table_list["direct"] = filtered_results

        # Add tables that matching OKF concepts point at
        self.__addOKFTables__(RetrievalObj)

    def __addOKFTables__(self, RetrievalObj):
        """
        Add tables referenced by OKF concepts relevant to the user's query, and
        keep those concepts for the prompt (self.okf_hits).

        Reuses the embedding model and ChromaDB client already loaded for table
        retrieval. Only tables that exist in table metadata for this instance are
        added, after the vector-search results. Concept bodies are cut to
        `okf.prompt_chars`; with `okf.include_in_prompt: false` no concepts are
        kept. No-op when the `okf` config section is missing or disabled, or
        when no bundle has been imported.
        """
        self.okf_hits = []
        try:
            okf_cfg = get_config_val("retrieval_config", ["okf"], True) or {}
        except Exception:
            return
        if not okf_cfg.get("enabled", False):
            return

        from MetadataManager.OKFStore import search_concepts, tables_from_concepts

        inst_filter = {}
        if self.instance_name != "default":
            inst_filter = {"instance_name": self.instance_name}
        rows = self.DBObj.get_data(
            self.tmddb_config['tableDescName'], inst_filter, ['tableName', 'Desc'], fetchtype="All",
        ) or []
        known = {row[0]: row[1] for row in rows if row and row[0]}
        if not known:
            return

        concepts = search_concepts(
            self.user_query,
            list(known),
            RetrievalObj.embedding_model,
            RetrievalObj.vdb.client,
            instance_name=self.instance_name,
            top_k=int(okf_cfg.get("top_k", 3)),
            max_distance=float(okf_cfg.get("max_distance", 0.5)),
        )

        for hit in tables_from_concepts(concepts):
            if hit["table"] in self.table_list["direct"]:
                continue
            self.table_list["direct"][hit["table"]] = {
                "description": known[hit["table"]] or hit["description"],
                "columns": {},
            }
            logger.debug(
                "OKF: added table %r via concept %r (distance %.3f)",
                hit["table"], hit["concept_id"], hit["distance"],
            )

        if okf_cfg.get("include_in_prompt", True):
            prompt_chars = int(okf_cfg.get("prompt_chars", 800))
            self.okf_hits = [
                {**c, "body": _truncate(c["body"], prompt_chars)} for c in concepts
            ]


    def __getTableRelations__(self):
        """
        Retrieve relations between tables.

        Returns:
            dict: A dictionary containing table relations and associated join keys.
        """
        # Initializing a Relations object for managing table relations
        RelationsObj = ManageRelations.Relations(strgType="kuzu", instance_name=self.instance_name)

        # Retrieving table relations and associated join keys; key columns let the
        # planner prefer key-to-key joins and label each hop's cardinality
        from MetadataManager.MetadataStore.relationdb.joinpath import load_unique_columns

        self.join_keys = RelationsObj.getRelation(
            list(self.table_list["direct"].keys()),
            unique_columns=load_unique_columns(self.instance_name),
        )

        # Updating the 'intermediate' table list attribute with tables not in the 'direct' table list
        for tables in self.join_keys:
            sourceTable = tables['source']
            targetTable = tables['target']

            if sourceTable not in self.table_list["direct"].keys() and sourceTable not in self.table_list["intermediate"].keys():
                self.table_list["intermediate"][sourceTable] = { "description": "", "columns": {} }

            if targetTable not in self.table_list["direct"].keys() and targetTable not in self.table_list["intermediate"].keys():
                self.table_list["intermediate"][targetTable] = { "description": "", "columns": {} }


    def __getInterTablesDesc__(self):
        """
        Get descriptions for intermediate tables.

        Returns:
            dict: A dictionary containing descriptions for intermediate tables.
        """
        # Updating descriptions for intermediate tables
        for table, _ in self.table_list["intermediate"].items():
            lookup = {'tableName': table}
            if self.instance_name != "default":
                lookup['instance_name'] = self.instance_name
            desc_row = self.DBObj.get_data(
                tableName=self.tmddb_config['tableDescName'],
                lookupDict=lookup,
                lookupVal=['Desc'],
            )
            self.table_list["intermediate"][table]["description"] = desc_row[0] if desc_row else ""

    def __filterAdditionalColumns__(self, col_tuples: list) -> list:
        """
        Filter table columns by relevance to the user query using BM25 keyword scoring.

        Key columns (PRIMARY KEY / FOREIGN KEY) are always retained regardless of score,
        as they are required for JOIN resolution. All other columns are kept only if their
        BM25 score against the user query exceeds the configured threshold.

        Args:
            col_tuples (list): List of (ColumnName, DataType, Constraints, Desc) tuples
                               as returned by SQLite fetchall().

        Returns:
            list: Filtered list of column tuples. Falls back to all columns if none pass.
        """
        if not col_tuples:
            return col_tuples

        threshold = get_config_val("retrieval_config", ["scoring", "column_score_threshold"])
        query_tokens = self.user_query.lower().split()

        # Build BM25 corpus: column name + description for each column.
        # Convert to a tuple of tuples so it can be used as an lru_cache key.
        corpus_key = tuple(
            tuple(f"{col[0] or ''} {col[3] or ''}".lower().split())
            for col in col_tuples
        )
        bm25 = _build_bm25(corpus_key)
        scores = bm25.get_scores(query_tokens)

        filtered = []
        for col, score in zip(col_tuples, scores):
            constraints = (col[2] or "").upper()
            is_key = "PRIMARY KEY" in constraints or "FOREIGN KEY" in constraints
            if score > threshold or is_key:
                filtered.append(col)

        if not filtered:
            logger.debug("Column filter: no columns passed threshold — returning all %d columns", len(col_tuples))
            return col_tuples

        logger.debug("Column filter: kept %d of %d columns", len(filtered), len(col_tuples))
        return filtered

    def __getTablesColList__(self):
        """
        Placeholder method to get the list of columns for each table.

        Args:
            table_list (list): A list of tables.

        Returns:
            dict: A dictionary containing the list of columns for each table.
        """
        # Iterating over table types and their corresponding dictionaries

        for ttype, table_dict in self.table_list.items():
            for table, tablemd in table_dict.items():
                col_lookup = {'TableName': table}
                if self.instance_name != "default":
                    col_lookup['instance_name'] = self.instance_name
                # Extracting column metadata for the current table
                fullColMetadata = self.DBObj.get_data(
                    tableName=self.tmddb_config['tableColName'],
                    lookupDict=col_lookup,
                    lookupVal=['ColumnName', 'DataType', 'Constraints', 'Desc'],
                    fetchtype="All"
                )
                # Updating the 'columns' attribute for the current table after filtering additional columns
                self.table_list[ttype][table]['columns'] = self.__filterAdditionalColumns__(fullColMetadata)


    def getBuildComponents(self, user_query: str, instance_name: str = None) -> dict:
        """
        Get components necessary for building the SQL query.

        Args:
            user_query (str): The user's SQL query.

        Returns:
            dict: A dictionary containing the following components:
                - user_query: The user's SQL query.
                - table_list: Relevant tables, both direct and intermediate.
                - join_keys: Table relations and associated join keys.
                - table_metadata: Metadata information for tables.
        """

        self.user_query = user_query
        if instance_name is not None:
            self.instance_name = instance_name

        # Get relevant tables
        self.__getRelevantTables__()
        logger.debug("Scanned relevant tables: %s", self.table_list)

        # Get relations between tables
        self.__getTableRelations__()
        logger.debug("Scanned table relations: %s", self.join_keys)

        # Get info for intermediate tables
        self.__getInterTablesDesc__()
        logger.debug("Scanned intermediate tables: %s", self.table_list)

        # Get relevant column list
        self.__getTablesColList__()
        logger.debug("Scanned all table metadata: %s", self.table_metadata)

        return {
            "user_query": self.user_query,
            "table_list": self.table_list,
            "join_keys": self.join_keys,
            "okf_hits": getattr(self, "okf_hits", []),
        }
