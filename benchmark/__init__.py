"""
benchmark — Poly-QL evaluation harness for text-to-SQL benchmarks.

Supported benchmarks:
  - BIRD  (https://bird-bench.github.io/)
  - Spider (https://yale-seas.github.io/spider/)

Workflow:
  1. python -m benchmark.ingest_bird  --data_dir <bird_dir>  [--instance_prefix bird]
  2. python -m benchmark.ingest_spider --data_dir <spider_dir> [--instance_prefix spider]
  3. python -m benchmark.run_inference --benchmark bird|spider --data_dir <dir> ...
  4. python -m benchmark.evaluate      --results_file <jsonl>  --benchmark bird|spider
"""
