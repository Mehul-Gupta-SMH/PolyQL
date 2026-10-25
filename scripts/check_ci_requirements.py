#!/usr/bin/env python3
"""Check that requirements-ci.txt and pyproject.toml agree on dependencies.

Rules (CI7):
  1. Every package in requirements-ci.txt must be declared in pyproject.toml
     (``dependencies`` or any ``optional-dependencies`` group), except the
     CI-only tools in ``CI_ONLY``.
  2. Every runtime dependency (``[project].dependencies``) in pyproject.toml
     must appear in requirements-ci.txt, except the packages in ``NOT_IN_CI``.

Stdlib only (Python 3.11+ for tomllib). Exit status 0 on success, 1 on mismatch.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# In requirements-ci.txt but intentionally not a project dependency.
CI_ONLY = {
    "ruff": "linter, run by CI only",
}

# Runtime deps intentionally absent from requirements-ci.txt (PEP 503 names).
NOT_IN_CI = {
    "chromadb": "mocked in tests/conftest.py",
    "sentence-transformers": "mocked in tests/conftest.py",
    "flagembedding": "mocked in tests/conftest.py",
    "torch": "mocked in tests/conftest.py (transitive of the embedding packages)",
    "pyvis": "mocked in tests/conftest.py",
    "openai": "no module-level import in tested code; LLM calls go through requests",
    "anthropic": "no module-level import in tested code; LLM calls go through requests",
    "tenacity": "not imported by any tested module",
    "python-dotenv": "not imported by any tested module",
    "uvicorn": "server runner only; tests use the FastAPI test client",
    "starlette": "installed transitively by fastapi",
}

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalise(name: str) -> str:
    """PEP 503 normalisation: lowercase, runs of -_. collapse to '-'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(line: str) -> str | None:
    """Return the normalised package name of a requirement line, or None.

    Ignores comments, blank lines, pip options, extras, markers and versions.
    """
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    m = _NAME_RE.match(line)
    return normalise(m.group(1)) if m else None


def parse_requirements(path: Path) -> set[str]:
    names = (requirement_name(ln) for ln in path.read_text(encoding="utf-8").splitlines())
    return {n for n in names if n}


def parse_pyproject(path: Path) -> tuple[set[str], set[str]]:
    """Return (runtime dependency names, all declared names incl. optional)."""
    project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    runtime = {n for d in project.get("dependencies", []) if (n := requirement_name(d))}
    declared = set(runtime)
    for group in project.get("optional-dependencies", {}).values():
        declared |= {n for d in group if (n := requirement_name(d))}
    return runtime, declared


def check(
    ci: set[str],
    runtime: set[str],
    declared: set[str],
    ci_only: set[str] | None = None,
    not_in_ci: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (in CI but undeclared in pyproject, runtime deps missing from CI)."""
    ci_only = {normalise(n) for n in (CI_ONLY if ci_only is None else ci_only)}
    not_in_ci = {normalise(n) for n in (NOT_IN_CI if not_in_ci is None else not_in_ci)}
    undeclared = sorted(ci - declared - ci_only)
    missing = sorted(runtime - ci - not_in_ci)
    return undeclared, missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--requirements", type=Path, default=ROOT / "requirements-ci.txt")
    ap.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    args = ap.parse_args(argv)

    ci = parse_requirements(args.requirements)
    runtime, declared = parse_pyproject(args.pyproject)
    undeclared, missing = check(ci, runtime, declared)

    if not undeclared and not missing:
        print(f"OK: {args.requirements.name} and {args.pyproject.name} are in sync "
              f"({len(ci)} CI packages, {len(runtime)} runtime deps).")
        return 0

    for name in undeclared:
        print(f"ERROR: '{name}' is in {args.requirements.name} but not declared in "
              f"{args.pyproject.name} (add it to dependencies or an optional group, "
              f"or to CI_ONLY in {Path(__file__).name}).")
    for name in missing:
        print(f"ERROR: '{name}' is a runtime dependency in {args.pyproject.name} but missing "
              f"from {args.requirements.name} (add it, or add it to NOT_IN_CI in "
              f"{Path(__file__).name} with a reason).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
