"""
benchmark/_bench_utils.py — shared helpers for ingest, inference, and evaluation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Instance naming
# ---------------------------------------------------------------------------

def make_instance_name(prefix: str, db_id: str) -> str:
    """Return a Poly-QL instance_name for a benchmark database."""
    return f"{prefix}_{db_id}"


# ---------------------------------------------------------------------------
# SQLite execution
# ---------------------------------------------------------------------------

def execute_sql_safe(
    db_path: Path,
    sql: str,
    timeout: float = 30.0,
) -> tuple[list[tuple], str | None]:
    """
    Execute *sql* against the SQLite file at *db_path*.

    Returns (rows, error_msg).  On success error_msg is None.
    On any exception rows is [] and error_msg carries the reason.
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=timeout)
        conn.row_factory = None
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        conn.close()
        return rows, None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


# ---------------------------------------------------------------------------
# Result-set normalisation (for Execution Accuracy)
# ---------------------------------------------------------------------------

def normalise_result(rows: list[tuple]) -> frozenset[frozenset]:
    """
    Convert a result set into an order-insensitive, type-normalised form.

    Each row becomes a frozenset of string values so that column order and
    numeric representation differences don't cause false negatives.
    """
    normalised = set()
    for row in rows:
        normalised.add(frozenset(str(v).strip().lower() for v in row))
    return frozenset(normalised)


def execution_match(
    pred_rows: list[tuple],
    gold_rows: list[tuple],
) -> bool:
    """Return True if normalised result sets are identical."""
    return normalise_result(pred_rows) == normalise_result(gold_rows)


# ---------------------------------------------------------------------------
# JSONL helpers
# ---------------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Poly-QL instance cleanup (for re-runs)
# ---------------------------------------------------------------------------

def clear_instance(instance_name: str) -> None:
    """
    Remove all SQLite metadata rows for *instance_name*.

    Silently skips tables that don't exist yet (first run).
    """
    from Utilities.base_utils import accessDB

    db = accessDB("table", "tableMetadata")
    for table in ("tableDesc", "tableColMetadata"):
        try:
            db.cursor.execute(
                f"DELETE FROM {table} WHERE instance_name = ?",  # noqa: S608
                (instance_name,),
            )
            db.connection.commit()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# BIRD difficulty lookup
# ---------------------------------------------------------------------------

def load_bird_difficulty(data_dir: Path) -> dict[str, str]:
    """
    Return {question_id: difficulty} from BIRD dev.json.

    BIRD questions carry a ``difficulty`` field ("simple", "moderate", "challenging").
    """
    dev_json = data_dir / "dev.json"
    if not dev_json.exists():
        return {}
    with dev_json.open(encoding="utf-8") as fh:
        questions = json.load(fh)
    return {str(q.get("question_id", i)): q.get("difficulty", "unknown")
            for i, q in enumerate(questions)}


# ---------------------------------------------------------------------------
# Column type inference
# ---------------------------------------------------------------------------

_NUMERIC_KEYWORDS = {
    "int", "integer", "bigint", "smallint", "tinyint",
    "float", "double", "real", "numeric", "decimal", "number",
}


def infer_col_type(raw_type: str) -> str:
    """Return a simplified type label from a raw DDL type string."""
    t = (raw_type or "").strip().lower().split("(")[0]
    if t in _NUMERIC_KEYWORDS:
        return "numeric"
    if "char" in t or "text" in t or "clob" in t or "string" in t:
        return "text"
    if "date" in t or "time" in t:
        return "datetime"
    if "bool" in t:
        return "boolean"
    if "blob" in t or "binary" in t:
        return "binary"
    return raw_type or "text"


# ---------------------------------------------------------------------------
# Progress logging helper
# ---------------------------------------------------------------------------

def log_progress(current: int, total: int, label: str = "items") -> None:
    pct = 100 * current // total if total else 0
    logger.info("[%d/%d] %d%% — %s processed", current, total, pct, label)


# ---------------------------------------------------------------------------
# Table existence check (avoid duplicate ingestion)
# ---------------------------------------------------------------------------

def table_already_ingested(table_name: str, instance_name: str) -> bool:
    """Return True if the table has already been stored for this instance."""
    from Utilities.base_utils import accessDB

    db = accessDB("table", "tableMetadata")
    rows = db.get_data(
        "tableDesc",
        {"tableName": table_name, "instance_name": instance_name},
        ["tableName"],
    )
    return len(rows) > 0
