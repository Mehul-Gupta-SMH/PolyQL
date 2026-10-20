"""
Process-wide embedding model, reranker and ChromaDB client.

Loading these is slow (seconds, hundreds of MB), so every call site shares one
instance instead of building its own per request. Each accessor is an
lru_cache, which does not cache exceptions: if loading fails, the next call
tries again. Heavy imports stay inside the functions so importing this module
is cheap and tests can patch the underlying classes.
"""

import functools


@functools.lru_cache(maxsize=1)
def get_embedding_model():
    """SentenceTransformer used for indexing and querying, loaded once."""
    from sentence_transformers import SentenceTransformer
    from Utilities.base_utils import get_config_val

    models_repo = get_config_val("retrieval_config", ["models_repo"], True)
    indexing_cfg = get_config_val("retrieval_config", ["indexing"], True)
    return SentenceTransformer(models_repo["path"] + "/" + indexing_cfg["model"])


@functools.lru_cache(maxsize=1)
def get_reranker():
    """FlagReranker cross-encoder, loaded once."""
    from FlagEmbedding import FlagReranker
    from Utilities.base_utils import get_config_val

    models_repo = get_config_val("retrieval_config", ["models_repo"], True)
    scoring_cfg = get_config_val("retrieval_config", ["scoring"], True)
    return FlagReranker(models_repo["path"] + "/" + scoring_cfg["crossencoder"], use_fp16=True)


@functools.lru_cache(maxsize=None)
def get_chroma_client(path: str):
    """Local persistent ChromaDB client for *path*, opened once per path."""
    from MetadataManager.MetadataStore.vdb.Chroma import getclient

    return getclient({"path": path}, session_type="local")


def reset() -> None:
    """Drop the loaded models and clients (tests; or to pick up a changed config)."""
    get_embedding_model.cache_clear()
    get_reranker.cache_clear()
    get_chroma_client.cache_clear()
