"""
benchmark/compare.py — compare evaluated benchmark runs question by question.

Typical use: measure what the extra context is worth by running the same
questions with and without it, evaluating each run, then comparing:

  python -m benchmark.run_inference ... --output_file results/full.jsonl
  python -m benchmark.run_inference ... --output_file results/no_okf.jsonl --without okf
  python -m benchmark.evaluate --results_file results/full.jsonl   ...
  python -m benchmark.evaluate --results_file results/no_okf.jsonl ...
  python -m benchmark.compare results/evaluated_full.jsonl results/evaluated_no_okf.jsonl

The first file is the baseline. Only questions present in every file are
compared. For each other run the report shows EX / VES, median latency, the
questions it got right that the baseline missed (won) and vice versa (lost),
and an exact McNemar p-value on those discordant pairs — with a few hundred
questions, a 1–2 point EX difference is often noise.
"""

from __future__ import annotations

import argparse
import json
import statistics
from math import comb
from pathlib import Path

from benchmark._bench_utils import read_jsonl


def mcnemar_p(won: int, lost: int) -> float:
    """Two-sided exact McNemar (binomial) p-value for the discordant pairs."""
    n = won + lost
    if n == 0:
        return 1.0
    k = min(won, lost)
    tail = sum(comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def _label(path: Path, records: list[dict]) -> str:
    without = next((r.get("without") for r in records if r.get("without")), None)
    name = path.stem.removeprefix("evaluated_")
    return f"{name} (without {','.join(without)})" if without else name


def compare(files: list[Path]) -> dict:
    """Compare evaluated_*.jsonl runs on their common questions; files[0] is the baseline."""
    runs = []
    for path in files:
        records = read_jsonl(path)
        if records and "ex" not in records[0]:
            raise ValueError(f"{path} has no 'ex' field — run benchmark.evaluate on it first")
        runs.append({"label": _label(path, records),
                     "by_q": {str(r["question_id"]): r for r in records}})

    common = set.intersection(*(set(r["by_q"]) for r in runs)) if runs else set()
    base = runs[0]["by_q"]
    report = {"questions": len(common), "runs": []}
    for run in runs:
        by_q = run["by_q"]
        rows = [by_q[q] for q in common]
        latencies = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
        entry = {
            "label": run["label"],
            "ex": round(100 * sum(bool(r["ex"]) for r in rows) / len(rows), 2) if rows else 0.0,
            "ves": round(100 * sum(bool(r["ves"]) for r in rows) / len(rows), 2) if rows else 0.0,
            "median_latency_ms": statistics.median(latencies) if latencies else None,
        }
        if run is not runs[0]:
            won = sorted(q for q in common if by_q[q]["ex"] and not base[q]["ex"])
            lost = sorted(q for q in common if base[q]["ex"] and not by_q[q]["ex"])
            entry.update({"won": won, "lost": lost, "mcnemar_p": round(mcnemar_p(len(won), len(lost)), 4)})
        report["runs"].append(entry)
    return report


def _print(report: dict, show: int) -> None:
    print("\n" + "=" * 72)
    print(f"Comparison on {report['questions']} common questions (first run = baseline)")
    print("=" * 72)
    base_ex = report["runs"][0]["ex"] if report["runs"] else 0.0
    for i, r in enumerate(report["runs"]):
        latency = f"{r['median_latency_ms']:.0f} ms" if r["median_latency_ms"] is not None else "n/a"
        line = f"  {r['label']:40s} EX {r['ex']:6.2f}%  VES {r['ves']:6.2f}%  latency {latency}"
        if i:
            line += (f"\n  {'':40s} ΔEX {r['ex'] - base_ex:+.2f}  won {len(r['won'])}  "
                     f"lost {len(r['lost'])}  McNemar p={r['mcnemar_p']}")
            if show:
                line += f"\n  {'':40s} won: {', '.join(r['won'][:show]) or '-'}"
                line += f"\n  {'':40s} lost: {', '.join(r['lost'][:show]) or '-'}"
        print(line)
    print("=" * 72 + "\n")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Compare evaluated benchmark runs (first = baseline).")
    parser.add_argument("files", nargs="+", type=Path, help="evaluated_*.jsonl files from benchmark.evaluate")
    parser.add_argument("--show", type=int, default=10, help="Question ids to list per won/lost (default 10).")
    parser.add_argument("--output_json", type=Path, default=None)
    args = parser.parse_args()
    if len(args.files) < 2:
        parser.error("give at least two evaluated result files")

    report = compare(args.files)
    _print(report, args.show)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    _cli()
