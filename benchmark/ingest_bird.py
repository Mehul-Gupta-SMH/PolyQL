"""
benchmark/ingest_bird.py — ingest BIRD benchmark schemas into Poly-QL.

BIRD directory layout (mini-dev or full dev):

  <data_dir>/
    dev_databases/
      <db_id>/
        <db_id>.sqlite
        database_description/
          <table_name>.csv   (col_name, data_format, value_description)
    dev_tables.json          (schema: column_names_original, primary_keys, foreign_keys …)
    dev.json                 (questions with evidence, difficulty)

Usage:
  python -m benchmark.ingest_bird \\
      --data_dir /path/to/bird/dev \\
      [--instance_prefix bird] \\
      [--db_ids concert_singer,pets]  # subset for quick tests
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from benchmark._bench_utils import (
    clear_instance,
    infer_col_type,
    log_progress,
    make_instance_name,
    table_already_ingested,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Description CSV helpers
# ---------------------------------------------------------------------------

def _load_table_descriptions(desc_dir: Path) -> dict[str, dict[str, str]]:
    """
    Read all CSV files in *desc_dir* and return::

        {table_name_lower: {col_name_lower: value_description}}
    """
    result: dict[str, dict[str, str]] = {}
    if not desc_dir.exists():
        return result
    for csv_path in desc_dir.glob("*.csv"):
        table_key = csv_path.stem.lower()
        col_descs: dict[str, str] = {}
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                with csv_path.open(encoding=encoding, newline="") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        col = (row.get("original_column_name") or row.get("col_name") or "").strip().lower()
                        desc = (row.get("column_description") or row.get("value_description") or row.get("description") or "").strip()
                        if col:
                            col_descs[col] = desc
                break  # success — no need to retry with next encoding
            except UnicodeDecodeError:
                col_descs = {}  # reset and retry
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not read %s: %s", csv_path, exc)
                break
        result[table_key] = col_descs
    return result


# ---------------------------------------------------------------------------
# Schema parsing (dev_tables.json / tables.json BIRD format)
# ---------------------------------------------------------------------------

def _parse_db_schema(db_entry: dict, desc_map: dict[str, dict[str, str]]) -> list[dict]:
    """
    Parse a single DB entry from BIRD's tables.json into a list of table dicts::

        [{
            "name": str,
            "desc": str,
            "columns": [{name, type, constraints, desc}],
            "relationships": [{source, target, join_keys}],
        }]
    """
    column_names = db_entry.get("column_names_original", [])
    column_types = db_entry.get("column_types", [])
    table_names  = db_entry.get("table_names_original", [])
    # primary_keys may contain plain ints OR nested lists (composite PKs)
    raw_pks = db_entry.get("primary_keys", [])
    primary_keys: set[int] = set()
    for pk in raw_pks:
        if isinstance(pk, list):
            primary_keys.update(pk)
        else:
            primary_keys.add(pk)
    foreign_keys = db_entry.get("foreign_keys", [])

    # Group columns by table index
    tables_cols: dict[int, list] = {i: [] for i in range(len(table_names))}
    for col_idx, (tbl_idx, col_name) in enumerate(column_names):
        if tbl_idx < 0:  # skip the special "*" column at index 0
            continue
        col_type = column_types[col_idx] if col_idx < len(column_types) else "text"
        constraints = "PRIMARY KEY" if col_idx in primary_keys else ""
        tbl_lower = table_names[tbl_idx].lower() if tbl_idx < len(table_names) else ""
        col_desc = desc_map.get(tbl_lower, {}).get(col_name.lower(), "")
        tables_cols[tbl_idx].append({
            "name": col_name,
            "type": infer_col_type(col_type),
            "constraints": constraints,
            "desc": col_desc,
        })

    # Build relationships from foreign_keys: [[col_idx_a, col_idx_b], ...]
    relationships: list[dict] = []
    seen_pairs: set[tuple] = set()
    for fk_pair in foreign_keys:
        if len(fk_pair) != 2:
            continue
        a_idx, b_idx = fk_pair
        if a_idx >= len(column_names) or b_idx >= len(column_names):
            continue
        a_tbl = column_names[a_idx][0]
        b_tbl = column_names[b_idx][0]
        a_col = column_names[a_idx][1]
        b_col = column_names[b_idx][1]
        if a_tbl < 0 or b_tbl < 0:
            continue
        src = table_names[a_tbl] if a_tbl < len(table_names) else ""
        tgt = table_names[b_tbl] if b_tbl < len(table_names) else ""
        pair = (min(src, tgt), max(src, tgt))
        if pair not in seen_pairs and src and tgt:
            seen_pairs.add(pair)
            relationships.append({
                "source": src,
                "target": tgt,
                "join_keys": f"{src}.{a_col} = {tgt}.{b_col}",
            })

    tables: list[dict] = []
    for tbl_idx, tbl_name in enumerate(table_names):
        cols = tables_cols.get(tbl_idx, [])

        # Build a rich description so the cross-encoder reranker can score it
        col_parts = []
        for c in cols[:12]:
            part = c["name"]
            if c.get("constraints"):
                part += f" ({c['constraints'].lower()})"
            if c.get("desc"):
                part += f": {c['desc']}"
            col_parts.append(part)
        if len(cols) > 12:
            col_parts.append(f"and {len(cols) - 12} more columns")

        tbl_desc = (
            f"The {tbl_name} table stores data about {tbl_name.replace('_', ' ')}. "
            f"Columns: {', '.join(col_parts)}."
        )

        tables.append({
            "name": tbl_name,
            "desc": tbl_desc,
            "columns": cols,
            "relationships": relationships,  # same list per table; store_table deduplicates
        })
    return tables


# ---------------------------------------------------------------------------
# Main ingestion routine
# ---------------------------------------------------------------------------

def ingest_bird(
    data_dir: Path,
    instance_prefix: str = "bird",
    db_ids: list[str] | None = None,
    force: bool = False,
) -> None:
    """
    Parse BIRD tables.json and ingest each database into Poly-QL.

    :param data_dir:        Root of the BIRD data directory (contains dev_tables.json).
    :param instance_prefix: Prefix for instance_name (default "bird").
    :param db_ids:          Optional subset of db_ids to ingest.
    :param force:           If True, drop existing metadata before re-ingesting.
    """
    tables_json = data_dir / "dev_tables.json"
    if not tables_json.exists():
        tables_json = data_dir / "tables.json"
    if not tables_json.exists():
        logger.error("No tables.json / dev_tables.json found in %s", data_dir)
        sys.exit(1)

    with tables_json.open(encoding="utf-8") as fh:
        all_db_entries: list[dict] = json.load(fh)

    # Filter to requested db_ids
    if db_ids:
        db_ids_set = set(db_ids)
        all_db_entries = [e for e in all_db_entries if e.get("db_id") in db_ids_set]

    logger.info("Ingesting %d BIRD databases …", len(all_db_entries))

    from backend.ingestion import store_table  # lazy: avoid startup overhead

    for idx, db_entry in enumerate(all_db_entries, 1):
        db_id = db_entry.get("db_id", f"db_{idx}")
        instance_name = make_instance_name(instance_prefix, db_id)
        log_progress(idx, len(all_db_entries), label=f"DBs (current: {db_id})")

        if force:
            clear_instance(instance_name)

        desc_dir = data_dir / "dev_databases" / db_id / "database_description"
        desc_map = _load_table_descriptions(desc_dir)

        tables = _parse_db_schema(db_entry, desc_map)

        for tbl in tables:
            if not force and table_already_ingested(tbl["name"], instance_name):
                logger.debug("  skip %s.%s (already ingested)", instance_name, tbl["name"])
                continue
            try:
                store_table(
                    table_name=tbl["name"],
                    table_desc=tbl["desc"],
                    columns=tbl["columns"],
                    relationships=tbl["relationships"],
                    instance_name=instance_name,
                    db_type="sqlite",
                )
                logger.debug("  stored %s.%s (%d cols)", instance_name, tbl["name"], len(tbl["columns"]))
            except Exception as exc:  # noqa: BLE001
                logger.error("  FAILED %s.%s: %s", instance_name, tbl["name"], exc)

    logger.info("BIRD ingestion complete.")


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Ingest BIRD benchmark schemas into Poly-QL.")
    parser.add_argument("--data_dir", required=True, type=Path,
                        help="Path to BIRD dev (or mini_dev) directory.")
    parser.add_argument("--instance_prefix", default="bird",
                        help="Prefix for Poly-QL instance_name (default: bird).")
    parser.add_argument("--db_ids", default="",
                        help="Comma-separated list of db_ids to ingest (default: all).")
    parser.add_argument("--force", action="store_true",
                        help="Drop and re-ingest existing metadata.")
    args = parser.parse_args()

    db_ids = [x.strip() for x in args.db_ids.split(",") if x.strip()] or None
    ingest_bird(args.data_dir, args.instance_prefix, db_ids, args.force)


if __name__ == "__main__":
    _cli()
