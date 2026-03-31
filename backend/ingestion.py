"""
Backend helpers for the Ingest Table workflow.

  parse_pipeline()    - Parse INSERT...SELECT or CREATE TABLE AS SELECT
  get_source_schema() - Fetch source table column metadata from SQLite
  store_table()       - Persist to SQLite, ChromaDB, and NetworkX
"""

import re
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _split_top_level_commas(text: str) -> list:
    """Split text by commas not nested inside parentheses."""
    parts, current, depth = [], [], 0
    for ch in text:
        if ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())
    return [p for p in parts if p]


def _find_top_level_from(text: str) -> int:
    """Return the index of the first top-level FROM keyword (depth 0)."""
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0:
            m = re.match(r'\bFROM\b', text[i:], re.IGNORECASE)
            if m:
                return i
        i += 1
    return -1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_pipeline(sql_text: str) -> dict:
    """
    Parse an INSERT...SELECT or CREATE TABLE AS SELECT pipeline query.

    Supports:
      INSERT INTO target (col1, col2) SELECT expr1, expr2 FROM ...
      CREATE TABLE [IF NOT EXISTS] target AS SELECT col1, col2 FROM ...

    Returns:
        {
            "target_table": str,
            "column_mappings": [{"target": str, "source_expr": str}, ...],
            "source_tables": [str, ...]  # existing tables from FROM/JOIN clauses
        }

    Raises ValueError if the SQL does not match either pattern.
    """
    sql = sql_text.strip()

    # --- Detect INSERT INTO target (cols) SELECT ... -------------------------
    # Capture unquoted qualified names (db.schema.table) and quoted plain names.
    insert_m = re.search(
        r'INSERT\s+(?:INTO\s+)?(?:`([^`]+)`|"([^"]+)"|\[([^\]]+)\]|([\w]+(?:\.[\w]+)*))\s*\(([^)]+)\)',
        sql, re.IGNORECASE,
    )
    # --- Detect CREATE TABLE target AS SELECT ... ----------------------------
    ctas_m = re.search(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:`([^`]+)`|"([^"]+)"|\[([^\]]+)\]|([\w]+(?:\.[\w]+)*))\s+AS\s+',
        sql, re.IGNORECASE,
    )

    if not insert_m and not ctas_m:
        raise ValueError(
            "Expected INSERT INTO … SELECT or CREATE TABLE … AS SELECT. "
            "Neither pattern was found."
        )

    def _extract_name(m):
        """Return the first non-None capture group (handles quoted + unquoted forms)."""
        return next(g for g in m.groups() if g is not None)

    if insert_m:
        target_table = _extract_name(insert_m)
        # columns list is the last group (group 5)
        explicit_cols = [c.strip() for c in insert_m.group(5).split(',')]
        tail = sql[insert_m.end():]
    else:
        target_table = _extract_name(ctas_m)
        explicit_cols = None
        tail = sql[ctas_m.end():]

    # --- Find SELECT in tail -------------------------------------------------
    sel_m = re.search(r'\bSELECT\b', tail, re.IGNORECASE)
    if not sel_m:
        raise ValueError("No SELECT clause found.")

    after_select = tail[sel_m.end():]

    # --- Find top-level FROM -------------------------------------------------
    from_idx = _find_top_level_from(after_select)
    if from_idx == -1:
        raise ValueError("No FROM clause found in SELECT statement.")

    select_expr_block = after_select[:from_idx].strip()
    from_onwards = after_select[from_idx:]

    # --- Parse SELECT expressions --------------------------------------------
    raw_exprs = _split_top_level_commas(select_expr_block)

    parsed_exprs = []
    for expr in raw_exprs:
        expr = expr.strip()
        alias_m = re.search(r'\bAS\s+[`"\[]?(\w+)[`"\]]?\s*$', expr, re.IGNORECASE)
        if alias_m:
            alias = alias_m.group(1)
        else:
            parts = expr.split()
            # Treat last token as alias only if it looks like a plain identifier
            # and is not inside a function call (no trailing paren)
            if (len(parts) >= 2
                    and re.match(r'^\w+$', parts[-1])
                    and not expr.rstrip().endswith(')')):
                alias = parts[-1]
            else:
                alias = None
        parsed_exprs.append({'expr': expr, 'alias': alias})

    # --- Build column mappings -----------------------------------------------
    column_mappings = []
    if explicit_cols:
        for i, tc in enumerate(explicit_cols):
            src = parsed_exprs[i]['expr'] if i < len(parsed_exprs) else ''
            column_mappings.append({'target': tc, 'source_expr': src})
    else:
        for i, pe in enumerate(parsed_exprs):
            col_name = pe['alias'] or f'col_{i + 1}'
            column_mappings.append({'target': col_name, 'source_expr': pe['expr']})

    # --- Find source tables (FROM + JOINs at top level) ----------------------
    _SKIP = {
        'select', 'where', 'on', 'and', 'or', 'not', 'null',
        'true', 'false', 'lateral', 'outer', 'inner', 'cross',
        'full', 'left', 'right', 'natural',
    }
    table_re = re.compile(
        r'(?:FROM|JOIN)\s+(`[^`]+`|"[^"]+"|\[[^\]]+\]|([\w]+(?:\.[\w]+)*))',
        re.IGNORECASE,
    )
    source_tables, seen = [], set()
    for m in table_re.finditer(from_onwards):
        raw = (m.group(2) or m.group(1)).strip('`"[]')
        name = raw.lower()
        # Skip SQL keywords and subquery aliases that start with '('
        if name not in _SKIP and name not in seen and not raw.startswith('('):
            seen.add(name)
            source_tables.append(name)

    return {
        'target_table': target_table,
        'column_mappings': column_mappings,
        'source_tables': source_tables,
    }


def get_source_schema(source_tables: list, instance_name: str = "default") -> dict:
    """
    Fetch column metadata from SQLite for each source table.

    Returns:
        {
            table_name: {
                "description": str,
                "columns": [{"name": str, "type": str, "desc": str}, ...]
            }
        }
    Tables not found in the metadata store are returned with empty descriptions.
    """
    from Utilities.base_utils import accessDB

    db = accessDB("table", "tableMetadata")
    schema = {}
    for table in source_tables:
        desc_lookup = {"tableName": table}
        col_lookup = {"TableName": table}
        if instance_name != "default":
            desc_lookup["instance_name"] = instance_name
            col_lookup["instance_name"] = instance_name
        desc_row = db.get_data("tableDesc", desc_lookup, ["Desc"])
        cols = db.get_data(
            "tableColMetadata", col_lookup,
            ["ColumnName", "DataType", "Desc"], fetchtype="All",
        ) or []
        schema[table] = {
            "description": (desc_row[0] if desc_row else "") or "",
            "columns": [
                {"name": c[0] or "", "type": c[1] or "", "desc": c[2] or ""}
                for c in cols
            ],
        }
    return schema


def format_source_schemas(schema: dict) -> str:
    """Format source schema dict into a readable string for the LLM prompt."""
    lines = []
    for table, info in schema.items():
        lines.append(f"=== {table} ===")
        if info["description"]:
            lines.append(f"Description: {info['description']}")
        if info["columns"]:
            lines.append("Columns:")
            for col in info["columns"]:
                desc_part = f": {col['desc']}" if col["desc"] else ""
                lines.append(f"  - {col['name']} ({col['type']}){desc_part}")
        else:
            lines.append("  (no column metadata found in schema)")
        lines.append("")
    return "\n".join(lines)


def format_column_mappings(column_mappings: list) -> str:
    """Format column mappings list into a readable string for the LLM prompt."""
    lines = []
    max_len = max((len(m["target"]) for m in column_mappings), default=10)
    for m in column_mappings:
        lines.append(f"  {m['target'].ljust(max_len)}  ←  {m['source_expr']}")
    return "\n".join(lines)


def store_table(
    table_name: str,
    table_desc: str,
    columns: list,        # [{name, type, constraints, desc}]
    relationships: list,  # [{source, target, join_keys}]
    instance_name: str = "default",
    db_type: str = "generic",
) -> None:
    """
    Persist a new table's metadata to all storage backends:
      1. SQLite  — tableDesc + tableColMetadata rows
      2. ChromaDB — table description embedding for RAG retrieval
      3. NetworkX — relationship graph edges

    :param instance_name: Named database instance (e.g. "default", "snowflake_prod").
    :param db_type: Technology label (e.g. "generic", "snowflake", "databricks").
    """
    from Utilities.base_utils import accessDB
    from MetadataManager.MetadataBuilder.importExisting.importData import importDD
    from MetadataManager.MetadataStore.RAGPipeline import ManageInformation
    from MetadataManager.MetadataStore.ManageRelations import Relations

    vdb_metadata = {
        "collection_name": "tableScan",
        "sim_metric": "cosine",
        "n_chunks": 3,
    }

    # 1. SQLite ---------------------------------------------------------------
    importer = importDD()
    importer.createTable()

    db = accessDB("table", "tableMetadata")
    db.post_data("tableDesc", [{
        "tableName": table_name,
        "Desc": table_desc,
        "instance_name": instance_name,
        "db_type": db_type,
    }])

    records = [
        {
            "TableName": table_name,
            "ColumnName": col["name"],
            "DataType": col.get("type", ""),
            "Constraints": col.get("constraints", ""),
            "logic": col.get("source_expr", ""),      # pipeline source expression
            "type_of_logic": "pipeline" if col.get("source_expr") else "",
            "base_table": "",
            "Desc": col.get("desc", ""),
            "instance_name": instance_name,
            "db_type": db_type,
        }
        for col in columns
    ]
    db.post_data("tableColMetadata", records)

    # 2. ChromaDB -------------------------------------------------------------
    vdb = ManageInformation()
    vdb.initialize_client()
    vdb.add_new_data(
        table_desc,
        {"TableName": table_name, "ENV": db_type, "DB": instance_name, "TType": "System"},
        vdb_metadata,
    )

    # 3. Kuzu graph — always register the table as a node so JOIN path lookups
    #    never raise NodeNotFound, even when the table has no declared FKs.
    from MetadataManager.MetadataStore.relationdb import kuzuDB as _kuzuDB
    _conn = _kuzuDB._get_conn(instance_name)
    _kuzuDB._ensure_schema(_conn, instance_name)
    _kuzuDB._merge_node(_conn, table_name.lower())

    if relationships:
        rel_mgr = Relations(instance_name=instance_name)
        edges = [
            [r["source"].lower(), r["target"].lower(), r.get("join_keys", "")]
            for r in relationships
        ]
        rel_mgr.addRelation(edges)

    logger.info("Stored table '%s' (instance=%s, db_type=%s) with %d columns and %d relationships.",
                table_name, instance_name, db_type, len(columns), len(relationships))
