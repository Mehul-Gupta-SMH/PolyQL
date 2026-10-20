import logging
import os
import jsonschema
import json
from Utilities.base_utils import accessDB
from MetadataManager.MetadataStore.RAGPipeline import ManageInformation
from MetadataManager.retrieval_cache import clear as clear_retrieval_cache

logger = logging.getLogger(__name__)

vdb_metadata = {
    "collection_name": "tableScan",
    "sim_metric": "cosine",
    "n_chunks": 3
}

# Define the JSON schema
schema = {
    "type": "object",
    "properties": {
        "tableName": {"type": "string"},
        "tableDesc": {"type": "string"},
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "TableName": {"type": "string"},
                    "ColumnName": {"type": "string"},
                    "DataType": {"type": "string"},
                    "Constraints": {"type": "string"},
                    "logic": {"type": "string"},
                    "type_of_logic": {"type": "string"},
                    "base_table": {"type": "string"},
                    "Desc": {"type": "string"},
                },
                "required": ["TableName", "ColumnName", "DataType", "Constraints", "logic", "type_of_logic", "base_table", "Desc"]
            }
        }
    },
    "required": ["tableName", "tableDesc", "records"]
}

def validate_json(json_data):
    """
    Validate JSON data against a predefined schema.

    Args:
        json_data (dict): JSON data to be validated.

    Raises:
        ValueError: If the JSON data is not valid according to the schema.
    """
    try:
        jsonschema.validate(instance=json_data, schema=schema)
        logger.debug("JSON schema validation passed.")
    except jsonschema.exceptions.ValidationError as e:
        raise ValueError("JSON is not valid.") from e

class importDD:
    """
    Class to import data dictionary information into the database.
    """
    def __init__(self):
        """
        Initializes an instance of importDD class.
        """
        info_type = "table"
        dbName = "tableMetadata"
        self.tableDescName = "tableDesc"
        self.tableColName = "tableColMetadata"

        self.DBObj = accessDB(info_type, dbName)
        self.vdbObj = ManageInformation()

    def createTable(self):
        """
        Create database tables if they don't exist, then migrate existing tables
        to add instance_name and db_type columns (backward-compatible migration).
        """
        # Create Table Desc Table
        tableDescSchema = {
            'tableName': self.tableDescName,
            'columns': {
                'tableName': ['TEXT', 'PRIMARY KEY'],
                'Desc': ['TEXT', ''],
                'instance_name': ['TEXT', "DEFAULT 'default'"],
                'db_type': ['TEXT', "DEFAULT 'generic'"],
            }
        }
        self.DBObj.create_table(tableDescSchema)

        # Create Table Column Metadata Table
        tableColSchema = {
            'tableName': self.tableColName,
            'columns': {
                'TableName': ['TEXT', ''],
                'ColumnName': ['TEXT', ''],
                'DataType': ['TEXT', ''],
                'Constraints': ['TEXT', ''],
                'logic': ['TEXT', ''],
                'type_of_logic': ['TEXT', ''],
                'base_table': ['TEXT', ''],
                'Desc': ['TEXT', ''],
                'instance_name': ['TEXT', "DEFAULT 'default'"],
                'db_type': ['TEXT', "DEFAULT 'generic'"],
            }
        }
        self.DBObj.create_table(tableColSchema)

        # Migrate existing tables: add new columns if they don't already exist.
        # SQLite does not support IF NOT EXISTS for ALTER TABLE ADD COLUMN,
        # so we use try/except for each migration statement.
        conn = self.DBObj.get_connection() if hasattr(self.DBObj, 'get_connection') else None
        if conn is None:
            # Fall back to direct sqlite3 connection via the DB path
            try:
                import sqlite3
                from Utilities.base_utils import get_config_val
                db_path = get_config_val("retrieval_config", ["tableMDdb", "dbName"], True)
                if not db_path.endswith('.db'):
                    db_path = db_path + '.db'
                conn = sqlite3.connect(db_path)
                _close_conn = True
            except Exception:
                conn = None
                _close_conn = False
        else:
            _close_conn = False

        if conn is not None:
            for tbl in [self.tableDescName, self.tableColName]:
                for col, default in [("instance_name", "'default'"), ("db_type", "'generic'")]:
                    try:
                        conn.execute(
                            f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT DEFAULT {default}"
                        )
                        conn.commit()
                        logger.info("Migrated column %s.%s", tbl, col)
                    except Exception:
                        pass  # column already exists
            if _close_conn:
                conn.close()




    def importData(self, jsonFilePath: str):
        """
        Import data dictionary information into the database.

        Args:
            jsonFilePath (str): Path to the JSON file containing data dictionary information.

        """
        # Create table, if not exists
        self.createTable()

        with open(jsonFilePath, "r") as tableJsonFObj:
            importedJsonData = json.load(tableJsonFObj)

        validate_json(importedJsonData)

        tableDesc = [{
            "tableName": importedJsonData['tableName'],
            "Desc": importedJsonData['tableDesc']
        },]

        tableCol = importedJsonData['records']

        # print(tableDesc)
        # print(tableCol)

        try:
            try:
                self.DBObj.post_data(self.tableDescName, tableDesc)
                self.DBObj.post_data(self.tableColName, tableCol)
            finally:
                clear_retrieval_cache()


            tableMD = {
                "TableName": importedJsonData['tableName'],
                "ENV": "PROD",
                "DB": "NORTHWIND",
                "TType": "System"
            }

            # tableMD = {**tableMD, **tableAttr}

            # Index table description into VectorDB
            self.vdbObj.initialize_client()
            result = self.vdbObj.add_new_data(importedJsonData['tableDesc'], tableMD, vdb_metadata)
            logger.info("VDB ingest result: %s", result)

        except Exception as e:
            logger.error("Failed to import data: %s", str(e))


if __name__ == "__main__":
    importDataObj = importDD()

    # importDataObj.importData(r"C:\Users\mehul\Documents\Projects - GIT\Agents\Decompose KG from Code\pythonProject\CoderAssistants\sampleFiles\existingDD\Impressions_Table.JSON")
    # importDataObj.importData(r"C:\Users\mehul\Documents\Projects - GIT\Agents\Decompose KG from Code\pythonProject\CoderAssistants\sampleFiles\existingDD\Campaigns_Table.JSON")
    # importDataObj.importData(r"C:\Users\mehul\Documents\Projects - GIT\Agents\Decompose KG from Code\pythonProject\CoderAssistants\sampleFiles\existingDD\Actions_Table.JSON")


    base_path = r"C:\Users\mehul\Documents\Projects - GIT\Agents\Decompose KG from Code\pythonProject\CoderAssistants\sampleFiles\NorthWinds\DD"

    for files in os.listdir(base_path):
        importDataObj.importData(os.path.join(base_path, files))

    # vdbObj = ManageInformation()
    # vdbObj.initialize_client()
    #
    # # query = "Write me a query that calculates all the impressions for all campaigns."
    #
    # # query = """
    # # For their annual review of the company pricing strategy, the Product Team wants to look at the products that are currently being offered for a specific price range ($20 to $50). In order to help them they asked you to provide them with a list of products with the following information:
    # #
    # # their name
    # # their unit price
    # # """
    #
    # query = """
    # Give me product wise split for each territory.
    # """
    #
    # results = vdbObj.get_data(query, vdb_metadata)
    #
    # for vals in results.values():
    #     print(f"Table Name : {vals['metadata']['TableName']}")
    #     print(vals)
    #     print()