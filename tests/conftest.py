"""
conftest.py — pre-mock heavy ML packages so the test suite runs in CI
without installing torch, sentence-transformers, FlagEmbedding, or chromadb.

These libraries are only needed at runtime when actual models are loaded.
All tests mock the classes/functions that use them, so the modules just
need to be importable, not functional.
"""
import sys
from unittest.mock import MagicMock

_HEAVY_MODULES = [
    "FlagEmbedding",
    "sentence_transformers",
    "torch",
    "chromadb",
    "chromadb.api",
    "chromadb.api.types",
    "chromadb.config",
    "pyvis",
    "pyvis.network",
    "iteration_utilities",
    "sqlalchemy",
]

for _mod in _HEAVY_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# ---------------------------------------------------------------------------
# Override the auth dependency so protected endpoints work in tests without
# a real JWT token.
# ---------------------------------------------------------------------------
from backend.app import app  # noqa: E402
from backend.auth import get_current_user  # noqa: E402

_FAKE_USER = {"sub": "testuser", "uid": 1}

app.dependency_overrides[get_current_user] = lambda: _FAKE_USER


# ---------------------------------------------------------------------------
# Keep process-wide caches (retrieval results, loaded models/clients) from
# leaking between tests.
# ---------------------------------------------------------------------------
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_shared_state():
    from MetadataManager import retrieval_cache
    from MetadataManager.MetadataStore import shared_models

    from backend import rate_limit
    from APIManager import circuit_breaker

    retrieval_cache.clear()
    shared_models.reset()
    rate_limit.reset()
    circuit_breaker.reset()
    yield
    retrieval_cache.clear()
    shared_models.reset()
    rate_limit.reset()
    circuit_breaker.reset()
