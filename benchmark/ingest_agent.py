"""
benchmark/ingest_agent.py — LLM-powered universal benchmark ingestion agent.

Accepts ANY benchmark data directory, autonomously explores its format, and
ingests every database into PolyQL's metadata store (SQLite + ChromaDB + Kuzu).

Supported formats (auto-detected):
  - BIRD / Spider  : tables.json with column_names_original, primary_keys, foreign_keys
  - SQL schema     : CREATE TABLE statements in .sql files
  - CSV metadata   : one CSV per table with column name, type, description columns
  - Custom JSON    : any JSON/YAML schema the LLM can interpret

Usage:
    python -m benchmark.ingest_agent \\
        --data_dir /path/to/benchmark \\
        --instance_prefix mybench \\
        [--db_ids concert_singer,pets] \\
        [--force] \\
        [--model claude-opus-4-6]

Requirements:
    ANTHROPIC_API_KEY env var (or set in .env)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_list_directory(path: str) -> str:
    """List files and subdirectories at *path* (one level deep)."""
    p = Path(path)
    if not p.exists():
        return f"ERROR: path does not exist: {path}"
    entries = []
    try:
        for item in sorted(p.iterdir()):
            kind = "DIR" if item.is_dir() else "FILE"
            size = f" ({item.stat().st_size:,} bytes)" if item.is_file() else ""
            entries.append(f"  [{kind}] {item.name}{size}")
    except PermissionError as e:
        return f"ERROR: {e}"
    return "\n".join(entries) if entries else "(empty directory)"


def _tool_read_file(path: str, max_bytes: int = 32_000) -> str:
    """Read *path* and return its text content (truncated to *max_bytes*)."""
    p = Path(path)
    if not p.exists():
        return f"ERROR: file does not exist: {path}"
    try:
        text = p.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
        if len(p.read_bytes()) > max_bytes:
            text += f"\n... [truncated — file is {p.stat().st_size:,} bytes total]"
        return text
    except Exception as e:
        return f"ERROR reading {path}: {e}"


def _tool_store_table(
    table_name: str,
    table_desc: str,
    columns: list[dict],
    relationships: list[dict],
    instance_name: str,
    db_type: str,
    force: bool,
) -> str:
    """Ingest one table into PolyQL metadata (SQLite + ChromaDB + Kuzu)."""
    from benchmark._bench_utils import table_already_ingested
    from backend.ingestion import store_table

    if not force and table_already_ingested(table_name, instance_name):
        return f"SKIPPED (already ingested): {instance_name}.{table_name}"
    try:
        store_table(
            table_name=table_name,
            table_desc=table_desc,
            columns=columns,
            relationships=relationships,
            instance_name=instance_name,
            db_type=db_type,
        )
        return f"OK: stored {instance_name}.{table_name} ({len(columns)} cols, {len(relationships)} FK edges)"
    except Exception as exc:
        return f"ERROR storing {instance_name}.{table_name}: {exc}"


def _tool_clear_instance(instance_name: str) -> str:
    """Drop all PolyQL metadata rows and Kuzu graph file for *instance_name*."""
    from benchmark._bench_utils import clear_instance

    clear_instance(instance_name)
    kuzu_path = (
        Path("MetadataManager/MetadataStore/MetadataStorage/relationsdb/kuzudb")
        / instance_name
    )
    if kuzu_path.exists():
        kuzu_path.unlink()
        return f"Cleared instance (SQLite rows + Kuzu file): {instance_name}"
    return f"Cleared instance (SQLite rows only — no Kuzu file found): {instance_name}"


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def dispatch_tool(tool_name: str, tool_input: dict, force: bool) -> str:
    if tool_name == "list_directory":
        return _tool_list_directory(tool_input["path"])

    if tool_name == "read_file":
        max_bytes = tool_input.get("max_bytes", 32_000)
        return _tool_read_file(tool_input["path"], max_bytes)

    if tool_name == "store_table":
        return _tool_store_table(
            table_name=tool_input["table_name"],
            table_desc=tool_input["table_desc"],
            columns=tool_input.get("columns", []),
            relationships=tool_input.get("relationships", []),
            instance_name=tool_input["instance_name"],
            db_type=tool_input.get("db_type", "generic"),
            force=force,
        )

    if tool_name == "clear_instance":
        return _tool_clear_instance(tool_input["instance_name"])

    return f"ERROR: unknown tool '{tool_name}'"


# ---------------------------------------------------------------------------
# Tool schema definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_directory",
        "description": "List all files and subdirectories at a given path (one level deep).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative directory path to list."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file and return its text content. Useful for inspecting schema files, JSON, CSV, SQL, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read."},
                "max_bytes": {"type": "integer", "description": "Max bytes to read (default 32000)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "store_table",
        "description": (
            "Ingest one table into PolyQL's metadata store (SQLite + ChromaDB + Kuzu graph). "
            "Call this once per table after you have parsed its schema."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Exact table name as it appears in the benchmark schema.",
                },
                "table_desc": {
                    "type": "string",
                    "description": (
                        "1-2 sentence description of what this table stores. "
                        "Mention the table name and its key columns. "
                        "E.g. 'The concerts table stores concert events. Columns: concert_id (primary key), concert_name, theme, year, location.'"
                    ),
                },
                "columns": {
                    "type": "array",
                    "description": "List of column definitions.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":        {"type": "string", "description": "Column name."},
                            "type":        {"type": "string", "description": "Data type: text, numeric, integer, date, datetime, boolean."},
                            "constraints": {"type": "string", "description": "E.g. 'PRIMARY KEY', 'NOT NULL', or empty string."},
                            "desc":        {"type": "string", "description": "Column description (from CSV or inferred). Empty string if unknown."},
                        },
                        "required": ["name", "type", "constraints", "desc"],
                    },
                },
                "relationships": {
                    "type": "array",
                    "description": "Foreign-key edges from THIS table to other tables.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source":    {"type": "string", "description": "Source table name (this table)."},
                            "target":    {"type": "string", "description": "Referenced table name."},
                            "join_keys": {"type": "string", "description": "E.g. 'concerts.stadium_id = stadiums.stadium_id'."},
                        },
                        "required": ["source", "target", "join_keys"],
                    },
                },
                "instance_name": {
                    "type": "string",
                    "description": "PolyQL instance name, e.g. 'mybench_concert_singer'.",
                },
                "db_type": {
                    "type": "string",
                    "description": "Database technology: sqlite, mysql, postgresql, generic. Default: generic.",
                },
            },
            "required": ["table_name", "table_desc", "columns", "relationships", "instance_name"],
        },
    },
    {
        "name": "clear_instance",
        "description": (
            "Remove ALL existing PolyQL metadata for an instance (SQLite rows + Kuzu graph file). "
            "Use before force re-ingesting a database."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instance_name": {"type": "string", "description": "Instance name to clear, e.g. 'mybench_concert_singer'."}
            },
            "required": ["instance_name"],
        },
    },
]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_system_prompt(data_dir: Path, instance_prefix: str, db_ids: list[str] | None, force: bool) -> str:
    db_filter = f"Only ingest these db_ids: {', '.join(db_ids)}." if db_ids else "Ingest ALL databases found."
    force_note = (
        "FORCE mode is ON — call clear_instance() for each database before ingesting it."
        if force
        else "Skip tables already ingested (store_table handles this automatically)."
    )
    return f"""You are a benchmark ingestion agent for PolyQL, a text-to-SQL assistant.

Your goal: explore the benchmark directory at `{data_dir}`, understand its schema format,
and ingest every database into PolyQL's metadata store using the provided tools.

## Configuration
- Data directory : {data_dir}
- Instance prefix: {instance_prefix}
- DB filter      : {db_filter}
- Force re-ingest: {force_note}

## PolyQL instance naming
Each database gets its own PolyQL instance: `{{instance_prefix}}_{{db_id}}`.
Example: prefix=`mybench`, db_id=`concert_singer` → instance_name=`mybench_concert_singer`.

## Ingestion workflow
1. List the data directory to understand the layout.
2. Find the schema file (tables.json, dev_tables.json, schema.sql, *.csv, etc.).
3. For each database (filtered by db_ids if set):
   a. If force mode, call clear_instance() first.
   b. Parse all tables, columns (name/type/constraints/desc), and FK relationships.
   c. Call store_table() once per table.
4. After all tables are ingested, print a short summary.

## Supported schema formats (auto-detect)
- **BIRD / Spider** — `tables.json` or `dev_tables.json`:
    `column_names_original` = [[table_idx, col_name], ...]
    `column_types` = [type_str, ...]
    `table_names_original` = [name, ...]
    `primary_keys` = [col_idx, ...] or [[col_idx, ...], ...]  (flatten nested lists)
    `foreign_keys` = [[col_idx_a, col_idx_b], ...]
    Column descriptions in per-table CSV files under `database_description/`.

- **SQL schema files** — Parse CREATE TABLE statements for column names, types, constraints,
    and FOREIGN KEY / REFERENCES clauses for relationships.

- **CSV metadata** — One CSV per table with columns like:
    `column_name`, `data_type`, `description` (or similar).

- **Custom JSON / YAML** — Inspect the file and infer structure.

## Column types
Normalise all types to one of: text, numeric, integer, date, datetime, boolean.

## Table descriptions
Write a clear 1-2 sentence description that includes:
- What the table represents
- Its key columns (especially the PK and any important domain columns)
Example: "The stadiums table stores information about sports stadiums.
Columns: stadium_id (primary key), location, name, capacity, highest, lowest, average."

## Relationships
For FK edges, include ALL relationships that involve this table (as source OR target).
Use format: "child_table.fk_col = parent_table.pk_col".
Pass the same full relationships list to every table in the same database.

## Error handling
If a file cannot be read or a table fails to store, log the error and continue with the next.
"""


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

def run_agent(
    data_dir: Path,
    instance_prefix: str,
    db_ids: list[str] | None,
    force: bool,
    model: str,
) -> None:
    try:
        import anthropic
    except ImportError:
        logger.error("anthropic package not found. Run: pip install anthropic")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY environment variable is not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    system = build_system_prompt(data_dir, instance_prefix, db_ids, force)
    initial_prompt = (
        f"Please ingest the benchmark data from `{data_dir}` into PolyQL. "
        f"Use instance prefix `{instance_prefix}`. Start by listing the directory."
    )

    messages: list[dict] = [{"role": "user", "content": initial_prompt}]
    tool_calls_total = 0
    store_calls = 0

    logger.info("Starting ingestion agent (model=%s, data_dir=%s)", model, data_dir)

    while True:
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # Append assistant response to history
        messages.append({"role": "assistant", "content": response.content})

        # Print any text blocks
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(block.text)

        # Done?
        if response.stop_reason == "end_turn":
            break

        # Handle tool calls
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_calls_total += 1
                logger.info("  tool: %s(%s)", block.name,
                            ", ".join(f"{k}={repr(v)[:60]}" for k, v in block.input.items()
                                      if k not in ("columns", "relationships")))
                result_text = dispatch_tool(block.name, block.input, force)
                if block.name == "store_table" and result_text.startswith("OK"):
                    store_calls += 1
                    logger.info("    → %s", result_text)
                elif result_text.startswith("ERROR"):
                    logger.warning("    → %s", result_text)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        logger.warning("Unexpected stop_reason: %s — stopping.", response.stop_reason)
        break

    logger.info(
        "Ingestion agent finished. tool_calls=%d, tables_stored=%d",
        tool_calls_total, store_calls,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="LLM-powered universal benchmark ingestion agent for PolyQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--data_dir", required=True, type=Path,
                        help="Root directory of the benchmark dataset.")
    parser.add_argument("--instance_prefix", required=True,
                        help="Prefix for PolyQL instance_name (e.g. 'mybench').")
    parser.add_argument("--db_ids", default="",
                        help="Comma-separated list of db_ids to ingest (default: all).")
    parser.add_argument("--force", action="store_true",
                        help="Clear existing metadata and re-ingest from scratch.")
    parser.add_argument("--model", default="claude-opus-4-6",
                        help="Anthropic model to use (default: claude-opus-4-6).")
    args = parser.parse_args()

    db_ids = [x.strip() for x in args.db_ids.split(",") if x.strip()] or None
    run_agent(
        data_dir=args.data_dir.resolve(),
        instance_prefix=args.instance_prefix,
        db_ids=db_ids,
        force=args.force,
        model=args.model,
    )


if __name__ == "__main__":
    _cli()
