"""
benchmark/evaluate.py — compute EX (Execution Accuracy) for BIRD / Spider results.

Metrics:
  - EX  (Execution Accuracy): predicted result set == gold result set,
        compared as frozenset-of-frozensets (order-insensitive, value-normalised).
  - VES (Valid SQL rate): fraction of predictions that execute without error.

Reports:
  - Overall EX and VES.
  - Breakdown by difficulty (BIRD: simple / moderate / challenging;
                             Spider: easy / medium / hard / extra).
  - Breakdown by db_id.
  - Per-question detail appended to results JSONL (evaluated_* variant).

Usage:
  python -m benchmark.evaluate \\
      --results_file results/bird_gpt4o_mini.jsonl \\
      --benchmark bird \\
      --data_dir /path/to/bird/dev \\
      [--output_csv results/bird_gpt4o_mini_summary.csv]
"""

from __future__ import annotations

import argparse
import csv
import logging
from collections import defaultdict
from pathlib import Path

from benchmark._bench_utils import (
    execute_sql_safe,
    execution_match,
    read_jsonl,
    write_jsonl,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

def _find_db_path(data_dir: Path, benchmark: str, db_id: str) -> Path | None:
    """Return the path to the SQLite file for a given db_id."""
    candidates = [
        data_dir / "dev_databases" / db_id / f"{db_id}.sqlite",   # BIRD mini_dev
        data_dir / "database" / db_id / f"{db_id}.sqlite",         # Spider / BIRD full
        data_dir / db_id / f"{db_id}.sqlite",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def evaluate(
    results_file: Path,
    benchmark: str,
    data_dir: Path,
    output_csv: Path | None = None,
) -> dict:
    """
    Evaluate a results JSONL file.

    Returns a summary dict with overall and per-difficulty metrics.
    Also writes an *evaluated_* JSONL with per-question ex/ves flags.
    """
    records = read_jsonl(results_file)
    if not records:
        logger.error("No records found in %s", results_file)
        return {}

    logger.info("Evaluating %d records from %s …", len(records), results_file)

    evaluated: list[dict] = []

    # Aggregate buckets: {key: {ex_total, ves_total, count}}
    diff_stats: dict[str, dict] = defaultdict(lambda: {"ex": 0, "ves": 0, "n": 0})
    db_stats:   dict[str, dict] = defaultdict(lambda: {"ex": 0, "ves": 0, "n": 0})
    overall = {"ex": 0, "ves": 0, "n": 0}

    for rec in records:
        db_id        = rec.get("db_id", "")
        gold_sql     = rec.get("gold_sql", "")
        pred_sql     = rec.get("predicted_sql", "")
        difficulty   = rec.get("difficulty", "unknown")
        result_type  = rec.get("result_type", "sql")

        db_path = _find_db_path(data_dir, benchmark, db_id)
        if db_path is None:
            logger.warning("SQLite DB not found for db_id=%s — skipping", db_id)
            continue

        # Execute gold
        gold_rows, gold_err = execute_sql_safe(db_path, gold_sql)
        if gold_err:
            logger.debug("Gold SQL error for question_id=%s: %s", rec.get("question_id"), gold_err)

        # Execute predicted
        if result_type == "error" or not pred_sql.strip():
            pred_rows, pred_err = [], "empty or error"
        else:
            pred_rows, pred_err = execute_sql_safe(db_path, pred_sql)

        is_valid = pred_err is None
        is_match = is_valid and execution_match(pred_rows, gold_rows)

        # Update buckets
        for bucket in (diff_stats[difficulty], db_stats[db_id], overall):
            bucket["n"]   += 1
            bucket["ex"]  += int(is_match)
            bucket["ves"] += int(is_valid)

        evaluated.append({
            **rec,
            "ex":       is_match,
            "ves":      is_valid,
            "pred_err": pred_err,
            "gold_err": gold_err,
        })

    # Summary
    def _pct(num: int, den: int) -> float:
        return round(100 * num / den, 2) if den else 0.0

    summary = {
        "overall": {
            "n":    overall["n"],
            "ex":   _pct(overall["ex"],  overall["n"]),
            "ves":  _pct(overall["ves"], overall["n"]),
            "ex_raw":  overall["ex"],
            "ves_raw": overall["ves"],
        },
        "by_difficulty": {
            d: {"n": s["n"], "ex": _pct(s["ex"], s["n"]), "ves": _pct(s["ves"], s["n"])}
            for d, s in sorted(diff_stats.items())
        },
        "by_db": {
            d: {"n": s["n"], "ex": _pct(s["ex"], s["n"]), "ves": _pct(s["ves"], s["n"])}
            for d, s in sorted(db_stats.items())
        },
    }

    # Print summary
    print("\n" + "=" * 60)
    print(f"Benchmark: {benchmark.upper()}   Results: {results_file.name}")
    print("=" * 60)
    o = summary["overall"]
    print(f"  Total questions : {o['n']}")
    print(f"  EX (Exec. Acc.) : {o['ex']}%  ({o['ex_raw']}/{o['n']})")
    print(f"  VES (Valid SQL) : {o['ves']}%  ({o['ves_raw']}/{o['n']})")
    print("\n  By difficulty:")
    for diff, ds in summary["by_difficulty"].items():
        print(f"    {diff:15s}  n={ds['n']:4d}  EX={ds['ex']:6.2f}%  VES={ds['ves']:6.2f}%")
    print("=" * 60 + "\n")

    # Write evaluated JSONL
    eval_path = results_file.parent / f"evaluated_{results_file.name}"
    write_jsonl(eval_path, evaluated)
    logger.info("Per-question results written to %s", eval_path)

    # Optional CSV
    if output_csv:
        _write_summary_csv(output_csv, summary, benchmark, results_file)

    return summary


# ---------------------------------------------------------------------------
# CSV summary writer
# ---------------------------------------------------------------------------

def _write_summary_csv(
    output_csv: Path,
    summary: dict,
    benchmark: str,
    results_file: Path,
) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["benchmark", "results_file", "scope", "key", "n", "ex_pct", "ves_pct"])
        o = summary["overall"]
        writer.writerow([benchmark, results_file.name, "overall", "all",
                         o["n"], o["ex"], o["ves"]])
        for diff, ds in summary["by_difficulty"].items():
            writer.writerow([benchmark, results_file.name, "difficulty", diff,
                             ds["n"], ds["ex"], ds["ves"]])
        for db_id, ds in summary["by_db"].items():
            writer.writerow([benchmark, results_file.name, "db", db_id,
                             ds["n"], ds["ex"], ds["ves"]])
    logger.info("Summary CSV written to %s", output_csv)


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Poly-QL benchmark results.")
    parser.add_argument("--results_file", required=True, type=Path,
                        help="JSONL file produced by run_inference.py.")
    parser.add_argument("--benchmark", required=True, choices=["bird", "spider"],
                        help="Which benchmark the results correspond to.")
    parser.add_argument("--data_dir", required=True, type=Path,
                        help="Root of the benchmark data directory (to locate SQLite DBs).")
    parser.add_argument("--output_csv", default=None, type=Path,
                        help="Optional path to write a summary CSV.")
    args = parser.parse_args()

    evaluate(args.results_file, args.benchmark, args.data_dir, args.output_csv)


if __name__ == "__main__":
    _cli()
