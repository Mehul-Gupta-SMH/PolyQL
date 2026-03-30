"""
benchmark/ingest_spider.py — ingest Spider benchmark schemas into Poly-QL.

Spider directory layout:

  <data_dir>/
    database/
      <db_id>/
        <db_id>.sqlite
    tables.json   (schema: column_names_original, primary_keys, foreign_keys …)
    train_spider.json / dev.json  (questions)

Usage:
  python -m benchmark.ingest_spider \\
      --data_dir /path/to/spider \\
      [--instance_prefix spider] \\
      [--db_ids concert_singer,pets] \\
      [--split dev|train]
"""

from __future__ import annotations

import argparse
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
# Schema parsing (identical format to BIRD — reuse same logic)
# ---------------------------------------------------------------------------

def _parse_db_schema(db_entry: dict) -> list[dict]:
    """
    Parse a Spider tables.json entry into a list of table dicts.

    Spider and BIRD share the same JSON schema format.
    """
    column_names = db_entry.get("column_names_original", [])
    column_types = db_entry.get("column_types", [])
    table_names  = db_entry.get("table_names_original", [])
    primary_keys = set(db_entry.get("primary_keys", []))
    foreign_keys = db_entry.get("foreign_keys", [])

    tables_cols: dict[int, list] = {i: [] for i in range(len(table_names))}
    for col_idx, (tbl_idx, col_name) in enumerate(column_names):
        if tbl_idx < 0:
            continue
        col_type = column_types[col_idx] if col_idx < len(column_types) else "text"
        constraints = "PRIMARY KEY" if col_idx in primary_keys else ""
        tables_cols[tbl_idx].append({
            "name": col_name,
            "type": infer_col_type(col_type),
            "constraints": constraints,
            "desc": "",
        })

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
        col_summary = ", ".join(c["name"] for c in cols[:8])
        if len(cols) > 8:
            col_summary += f", … (+{len(cols) - 8} more)"
        tbl_desc = f"Table {tbl_name} with columns: {col_summary}."
        tables.append({
            "name": tbl_name,
            "desc": tbl_desc,
            "columns": cols,
            "relationships": relationships,
        })
    return tables


# ---------------------------------------------------------------------------
# Main ingestion routine
# ---------------------------------------------------------------------------

def ingest_spider(
    data_dir: Path,
    instance_prefix: str = "spider",
    db_ids: list[str] | None = None,
    force: bool = False,
) -> None:
    """
    Parse Spider tables.json and ingest each database into Poly-QL.

    :param data_dir:        Root of the Spider data directory (contains tables.json).
    :param instance_prefix: Prefix for instance_name (default "spider").
    :param db_ids:          Optional subset of db_ids to ingest.
    :param force:           If True, drop existing metadata before re-ingesting.
    """
    tables_json = data_dir / "tables.json"
    if not tables_json.exists():
        logger.error("tables.json not found in %s", data_dir)
        sys.exit(1)

    with tables_json.open(encoding="utf-8") as fh:
        all_db_entries: list[dict] = json.load(fh)

    if db_ids:
        db_ids_set = set(db_ids)
        all_db_entries = [e for e in all_db_entries if e.get("db_id") in db_ids_set]

    logger.info("Ingesting %d Spider databases …", len(all_db_entries))

    from backend.ingestion import store_table

    for idx, db_entry in enumerate(all_db_entries, 1):
        db_id = db_entry.get("db_id", f"db_{idx}")
        instance_name = make_instance_name(instance_prefix, db_id)
        log_progress(idx, len(all_db_entries), label=f"DBs (current: {db_id})")

        if force:
            clear_instance(instance_name)

        tables = _parse_db_schema(db_entry)

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
            except Exception as exc:  # noqa: BLE001
                logger.error("  FAILED %s.%s: %s", instance_name, tbl["name"], exc)

    logger.info("Spider ingestion complete.")


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Ingest Spider benchmark schemas into Poly-QL.")
    parser.add_argument("--data_dir", required=True, type=Path,
                        help="Path to Spider root directory (contains tables.json).")
    parser.add_argument("--instance_prefix", default="spider",
                        help="Prefix for Poly-QL instance_name (default: spider).")
    parser.add_argument("--db_ids", default="",
                        help="Comma-separated list of db_ids to ingest (default: all).")
    parser.add_argument("--force", action="store_true",
                        help="Drop and re-ingest existing metadata.")
    args = parser.parse_args()

    db_ids = [x.strip() for x in args.db_ids.split(",") if x.strip()] or None
    ingest_spider(args.data_dir, args.instance_prefix, db_ids, args.force)


if __name__ == "__main__":
    _cli()
