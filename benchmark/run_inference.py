"""
benchmark/run_inference.py — batch inference for BIRD and Spider benchmarks.

For each question the script:
  1. Builds a conversation list that optionally injects the BIRD "evidence" hint.
  2. Calls generateQuery() directly (bypasses gatherRequirements for speed).
  3. Writes one JSONL record per question to *output_file*.

The output file can be resumed — already-processed question_ids are skipped.

Usage:
  python -m benchmark.run_inference \\
      --benchmark bird \\
      --data_dir /path/to/bird/dev \\
      --provider open_ai \\
      --model gpt-4o-mini \\
      --output_file results/bird_gpt4o_mini.jsonl \\
      [--instance_prefix bird] \\
      [--limit 50]            # optional: only first N questions
      [--db_ids concert_singer,pets]
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from benchmark._bench_utils import (
    append_jsonl,
    make_instance_name,
    read_jsonl,
    write_jsonl,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Question loaders
# ---------------------------------------------------------------------------

def _load_bird_questions(data_dir: Path, db_ids: set[str] | None = None) -> list[dict]:
    """
    Load BIRD dev.json (or mini_dev.json) questions.

    Returns normalised list::

        [{question_id, db_id, question, evidence, gold_sql, difficulty}]
    """
    for name in ("dev.json", "mini_dev.json", "mini_dev_sqlite.json"):
        path = data_dir / name
        if path.exists():
            break
    else:
        raise FileNotFoundError(f"dev.json / mini_dev.json / mini_dev_sqlite.json not found in {data_dir}")

    with path.open(encoding="utf-8") as fh:
        raw: list[dict] = json.load(fh)

    questions = []
    for i, q in enumerate(raw):
        db_id = q.get("db_id", "")
        if db_ids and db_id not in db_ids:
            continue
        questions.append({
            "question_id": str(q.get("question_id", i)),
            "db_id": db_id,
            "question": q.get("question", ""),
            "evidence": q.get("evidence", ""),
            "gold_sql": q.get("SQL", q.get("query", "")),
            "difficulty": q.get("difficulty", "unknown"),
        })
    return questions


def _load_spider_questions(data_dir: Path, split: str, db_ids: set[str] | None = None) -> list[dict]:
    """
    Load Spider questions from dev.json or train_spider.json.

    Returns normalised list (no evidence field — set to "").
    """
    candidates = [
        data_dir / f"{split}.json",
        data_dir / f"train_{split}.json",
        data_dir / "dev.json",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        raise FileNotFoundError(f"Spider {split} questions not found in {data_dir}")

    with path.open(encoding="utf-8") as fh:
        raw: list[dict] = json.load(fh)

    questions = []
    for i, q in enumerate(raw):
        db_id = q.get("db_id", "")
        if db_ids and db_id not in db_ids:
            continue
        questions.append({
            "question_id": str(i),
            "db_id": db_id,
            "question": q.get("question", ""),
            "evidence": "",
            "gold_sql": q.get("query", ""),
            "difficulty": q.get("hardness", "unknown"),
        })
    return questions


# ---------------------------------------------------------------------------
# Conversation builder
# ---------------------------------------------------------------------------

def _build_conversation(question: str, evidence: str) -> list[dict]:
    """
    Build a minimal conversation list for generateQuery().

    Evidence (BIRD) is passed as an earlier assistant exchange so it reaches
    the LLM as context without polluting the ChromaDB RAG search query.
    The final user turn is always the plain question — generateQuery() uses the
    last user messages for vector search.
    """
    messages: list[dict] = []
    if evidence:
        messages.append({
            "role": "user",
            "content": f"Domain knowledge hint: {evidence}",
        })
        messages.append({
            "role": "assistant",
            "content": "Understood. I will apply that domain knowledge when writing the SQL.",
        })
    messages.append({"role": "user", "content": question})
    return messages


# ---------------------------------------------------------------------------
# Main inference loop
# ---------------------------------------------------------------------------

def run_inference(
    benchmark: str,
    data_dir: Path,
    provider: str,
    output_file: Path,
    model: str | None = None,
    instance_prefix: str | None = None,
    split: str = "dev",
    limit: int | None = None,
    db_ids: list[str] | None = None,
    sleep_between: float = 0.0,
) -> None:
    """
    Run batch inference and write results to *output_file* (JSONL).

    Resumes automatically if *output_file* already contains partial results.
    """
    from main import generateQuery  # noqa: PLC0415

    if instance_prefix is None:
        instance_prefix = benchmark

    # Load questions
    db_ids_set = set(db_ids) if db_ids else None
    if benchmark == "bird":
        questions = _load_bird_questions(data_dir, db_ids_set)
    elif benchmark == "spider":
        questions = _load_spider_questions(data_dir, split, db_ids_set)
    else:
        raise ValueError(f"Unknown benchmark: {benchmark!r}. Use 'bird' or 'spider'.")

    if limit:
        questions = questions[:limit]

    logger.info("Loaded %d questions for %s.", len(questions), benchmark)

    # Resume: collect successfully-processed question_ids (skip errors so they retry)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    all_prior = list(read_jsonl(output_file))
    done_ids: set[str] = {r["question_id"] for r in all_prior if r.get("result_type") == "sql"}
    if all_prior:
        logger.info("Resuming — %d questions already processed (%d successful, %d errors will retry).",
                    len(all_prior), len(done_ids), len(all_prior) - len(done_ids))
        # Rewrite file keeping only successful rows
        write_jsonl(output_file, [r for r in all_prior if r.get("result_type") == "sql"])

    processed = 0
    errors = 0

    for q in questions:
        qid = q["question_id"]
        if qid in done_ids:
            continue

        instance_name = make_instance_name(instance_prefix, q["db_id"])
        conversation  = _build_conversation(q["question"], q["evidence"])
        t0 = time.perf_counter()

        try:
            result = generateQuery(
                userQuery=q["question"],
                LLMservice=provider,
                query_type="sql",
                conversation=conversation,
                model=model,
                instance_name=instance_name,
            )
            predicted_sql = result.get("content", "") if isinstance(result, dict) else str(result)
            result_type   = result.get("type", "sql") if isinstance(result, dict) else "sql"
            error_msg     = None
        except Exception as exc:  # noqa: BLE001
            predicted_sql = ""
            result_type   = "error"
            error_msg     = str(exc)
            errors += 1
            logger.warning("  question_id=%s FAILED: %s", qid, exc)

        latency_ms = int((time.perf_counter() - t0) * 1000)

        record = {
            "question_id":  qid,
            "db_id":        q["db_id"],
            "question":     q["question"],
            "evidence":     q.get("evidence", ""),
            "gold_sql":     q["gold_sql"],
            "predicted_sql": predicted_sql,
            "result_type":  result_type,
            "error_msg":    error_msg,
            "difficulty":   q.get("difficulty", "unknown"),
            "provider":     provider,
            "model":        model or "",
            "instance_name": instance_name,
            "latency_ms":   latency_ms,
        }
        append_jsonl(output_file, record)
        processed += 1

        if processed % 10 == 0:
            logger.info("  processed %d / %d (errors: %d)", processed, len(questions), errors)

        if sleep_between > 0:
            time.sleep(sleep_between)

    logger.info(
        "Inference complete — %d processed, %d errors. Results: %s",
        processed, errors, output_file,
    )


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Batch inference for BIRD / Spider benchmarks.")
    parser.add_argument("--benchmark", required=True, choices=["bird", "spider"])
    parser.add_argument("--data_dir", required=True, type=Path)
    parser.add_argument("--provider", required=True,
                        help="Poly-QL LLM provider (open_ai, anthropic, google, groq, codex, claude_code).")
    parser.add_argument("--output_file", required=True, type=Path,
                        help="Path to write JSONL results.")
    parser.add_argument("--model", default=None,
                        help="Specific model override (optional).")
    parser.add_argument("--instance_prefix", default=None,
                        help="Instance prefix (default: same as --benchmark).")
    parser.add_argument("--split", default="dev",
                        help="Question split for Spider: dev or train (default: dev).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N questions.")
    parser.add_argument("--db_ids", default="",
                        help="Comma-separated db_ids to include (default: all).")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="Sleep seconds between calls (rate-limit guard).")
    args = parser.parse_args()

    db_ids = [x.strip() for x in args.db_ids.split(",") if x.strip()] or None
    run_inference(
        benchmark=args.benchmark,
        data_dir=args.data_dir,
        provider=args.provider,
        output_file=args.output_file,
        model=args.model,
        instance_prefix=args.instance_prefix,
        split=args.split,
        limit=args.limit,
        db_ids=db_ids,
        sleep_between=args.sleep,
    )


if __name__ == "__main__":
    _cli()
