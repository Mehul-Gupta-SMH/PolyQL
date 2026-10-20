"""
benchmark/mcp_coldstart.py — time the PolyQL MCP server from launch to first answer.

Each run starts a fresh server process over stdio (the way Claude Code, Codex
and most agent harnesses launch it) and records:

  initialize_ms      process spawn → MCP handshake complete (imports, tool registry)
  list_tools_ms      tools/list round trip
  first_search_ms    first search_schema call — loads the embedding model,
                     reranker, ChromaDB and Kuzu (the real cold start an agent sees)
  warm_search_ms     median of the following search_schema calls (models loaded)
  total_ms           spawn → first search_schema answered

Usage (run on a machine with ingested metadata):
  python -m benchmark.mcp_coldstart \\
      --question "monthly revenue by customer segment" \\
      [--instance default] [--runs 5] [--warm_calls 3] \\
      [--output_json results/mcp_coldstart.json]

Several --question flags may be given; the first is used for the cold call and
the warm calls cycle through all of them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
METRICS = ("initialize_ms", "list_tools_ms", "first_search_ms", "warm_search_ms", "total_ms")


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


async def measure_once(questions: list[str], instance: str, warm_calls: int,
                       server_args: list[str] | None = None) -> dict:
    """Start one server process and time the handshake and search_schema calls."""
    from mcp import Client, StdioServerParameters  # noqa: PLC0415

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "backend.mcp_server", *(server_args or [])],
        cwd=str(PROJECT_ROOT),
        env=dict(os.environ),
    )
    result: dict = {}
    start = time.perf_counter()
    async with Client(params) as client:
        result["initialize_ms"] = _ms(start)

        t = time.perf_counter()
        tools = await client.list_tools()
        result["list_tools_ms"] = _ms(t)
        result["tools"] = len(tools.tools)

        t = time.perf_counter()
        first = await client.call_tool("search_schema", {"question": questions[0], "instance": instance})
        result["first_search_ms"] = _ms(t)
        result["total_ms"] = _ms(start)
        result["first_search_error"] = bool(getattr(first, "is_error", False))
        text = "".join(getattr(c, "text", "") for c in first.content)
        result["context_chars"] = len(text)

        warm = []
        for i in range(warm_calls):
            t = time.perf_counter()
            await client.call_tool("search_schema", {
                "question": questions[(i + 1) % len(questions)], "instance": instance,
            })
            warm.append(_ms(t))
        result["warm_search_ms"] = round(statistics.median(warm), 1) if warm else None
    return result


def summarise(runs: list[dict]) -> dict:
    """Median, min and max of each metric across runs."""
    summary = {}
    for key in METRICS:
        values = [r[key] for r in runs if r.get(key) is not None]
        if values:
            summary[key] = {"median": round(statistics.median(values), 1),
                            "min": min(values), "max": max(values)}
    return summary


def run(questions: list[str], instance: str = "default", runs: int = 5, warm_calls: int = 3,
        server_args: list[str] | None = None) -> dict:
    import anyio  # noqa: PLC0415

    results = []
    for i in range(runs):
        r = anyio.run(measure_once, questions, instance, warm_calls, server_args)
        logger.info("run %d/%d: %s", i + 1, runs, {k: r.get(k) for k in METRICS})
        if r.get("first_search_error"):
            logger.warning("search_schema returned an error — is metadata ingested for %r?", instance)
        results.append(r)
    return {"instance": instance, "questions": questions, "runs": results, "summary": summarise(results)}


def _print(report: dict) -> None:
    print("\n" + "=" * 60)
    print(f"MCP cold start — instance {report['instance']!r}, {len(report['runs'])} runs")
    print("=" * 60)
    for key, s in report["summary"].items():
        print(f"  {key:16s} median {s['median']:9.1f} ms   (min {s['min']:.1f}, max {s['max']:.1f})")
    print("=" * 60 + "\n")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Time the PolyQL MCP server's cold start.")
    parser.add_argument("--question", action="append", required=True,
                        help="Question for search_schema (repeatable).")
    parser.add_argument("--instance", default="default")
    parser.add_argument("--runs", type=int, default=5, help="Fresh server processes to start (default 5).")
    parser.add_argument("--warm_calls", type=int, default=3,
                        help="search_schema calls after the first, per run (default 3).")
    parser.add_argument("--output_json", type=Path, default=None)
    args = parser.parse_args()

    report = run(args.question, args.instance, args.runs, args.warm_calls)
    _print(report)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, indent=2))
        logger.info("Report written to %s", args.output_json)


if __name__ == "__main__":
    _cli()
