"""
MetadataManager/GlossaryStore.py — SL0 + SL1: Business Glossary

SL0: SQLite CRUD for the `business_terms` table (lives in tableMetadata.db).
SL1: ChromaDB-backed semantic search via get_business_context().

All CRUD functions work without any ML dependencies.
index_term() and get_business_context() require sentence_transformers + chromadb
(pre-mocked in CI via conftest.py — same as the rest of the retrieval stack).
"""

import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Table schema — used by accessDB.create_table (IF NOT EXISTS)
# ---------------------------------------------------------------------------

_TABLE_SCHEMA = {
    "tableName": "business_terms",
    "columns": {
        "term_id":       ["TEXT", "PRIMARY KEY"],
        "term_name":     ["TEXT", "NOT NULL"],
        "full_name":     ["TEXT", ""],
        "definition":    ["TEXT", ""],
        "formula":       ["TEXT", ""],
        "formula_type":  ["TEXT", ""],
        "table_deps":    ["TEXT", ""],   # JSON array of table names
        "column_deps":   ["TEXT", ""],   # JSON array of "table.column" strings
        "synonyms":      ["TEXT", ""],   # JSON array of synonym strings
        "example_value": ["TEXT", ""],
        "domain":        ["TEXT", ""],
        "instance_name": ["TEXT", ""],
        "created_at":    ["TEXT", ""],
        "updated_at":    ["TEXT", ""],
    },
}

# Canonical column order (matches CREATE TABLE order for SELECT * unpacking)
_COLUMNS = list(_TABLE_SCHEMA["columns"].keys())
_JSON_FIELDS = ("table_deps", "column_deps", "synonyms")

# ChromaDB collection name for business glossary embeddings
_COLLECTION = "business_glossary"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _invalidate_retrieval_cache() -> None:
    from MetadataManager.retrieval_cache import clear
    clear()


def _get_db():
    """Open the shared tableMetadata.db and ensure business_terms table exists."""
    from Utilities.base_utils import accessDB, get_config_val

    tmddb = get_config_val("retrieval_config", ["tableMDdb"], True)
    db = accessDB(tmddb["info_type"], tmddb["dbName"])
    db.create_table(_TABLE_SCHEMA)
    return db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: tuple) -> dict:
    """Convert a raw SQLite SELECT-* row to a typed dict with JSON fields decoded."""
    d = dict(zip(_COLUMNS, row))
    for field in _JSON_FIELDS:
        raw = d.get(field) or "[]"
        try:
            d[field] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            d[field] = []
    return d


def _serialize(term: dict) -> dict:
    """Produce a flat string dict suitable for accessDB.post_data / update_data."""
    out: dict = {}
    for col in _COLUMNS:
        if col in ("term_id", "created_at", "updated_at"):
            continue
        v = term.get(col)
        if col in _JSON_FIELDS:
            out[col] = json.dumps(v if isinstance(v, list) else [])
        else:
            out[col] = str(v) if v is not None else ""
    return out


# ---------------------------------------------------------------------------
# SL0 — CRUD
# ---------------------------------------------------------------------------

def add_term(term: dict) -> str:
    """
    Insert a new business term. Returns the term_id (generated if absent).

    Raises ValueError when term_name is missing or empty.
    """
    if not str(term.get("term_name") or "").strip():
        raise ValueError("term_name is required")

    db = _get_db()
    term_id = str(term.get("term_id") or uuid.uuid4())
    now = _now()

    record = _serialize(term)
    record["term_id"] = term_id
    record["created_at"] = now
    record["updated_at"] = now

    db.post_data("business_terms", [record])
    _invalidate_retrieval_cache()
    logger.info("GlossaryStore: added term %r (id=%s)", term.get("term_name"), term_id)
    return term_id


def get_term(term_id: str) -> dict | None:
    """Fetch a single term by ID. Returns None if not found."""
    db = _get_db()
    row = db.get_data("business_terms", {"term_id": term_id}, [], fetchtype="one")
    return _row_to_dict(row) if row else None


def get_term_by_name(term_name: str, instance_name: str = "") -> dict | None:
    """
    Fetch a single term by exact name (case-insensitive).
    Pass instance_name="" to search across all instances.
    """
    db = _get_db()
    lookup: dict = {"term_name": term_name}
    if instance_name:
        lookup["instance_name"] = instance_name
    row = db.get_data("business_terms", lookup, [], fetchtype="one")
    return _row_to_dict(row) if row else None


def update_term(term_id: str, updates: dict) -> bool:
    """
    Update fields on an existing term by ID. Returns True on success.
    JSON list fields (table_deps, column_deps, synonyms) are serialised automatically.
    """
    db = _get_db()
    payload: dict = {}
    for k, v in updates.items():
        if k in ("term_id", "created_at"):
            continue
        if k in _JSON_FIELDS:
            payload[k] = json.dumps(v if isinstance(v, list) else [])
        else:
            payload[k] = str(v) if v is not None else ""
    payload["updated_at"] = _now()
    db.update_data("business_terms", {"term_id": term_id}, payload)
    _invalidate_retrieval_cache()
    logger.info("GlossaryStore: updated term id=%s fields=%s", term_id, list(payload.keys()))
    return True


def delete_term(term_id: str) -> None:
    """Delete a term by ID."""
    db = _get_db()
    db.delete_data("business_terms", {"term_id": term_id})
    _invalidate_retrieval_cache()
    logger.info("GlossaryStore: deleted term id=%s", term_id)


def list_terms(instance_name: str = None, domain: str = None) -> list:
    """
    Return all terms, optionally filtered by instance_name and/or domain.
    Pass None to omit a filter.
    """
    db = _get_db()
    lookup: dict = {}
    if instance_name is not None:
        lookup["instance_name"] = instance_name
    if domain is not None:
        lookup["domain"] = domain
    rows = db.get_data("business_terms", lookup, [], fetchtype="All") or []
    return [_row_to_dict(r) for r in rows]


def search_by_name(query: str, instance_name: str = None) -> list:
    """
    Lexical LIKE search on term_name and full_name.
    Returns terms where either field contains *query* (case-insensitive).
    """
    db = _get_db()
    pattern = f"%{query}%"
    sql = (
        "SELECT * FROM business_terms "
        "WHERE (lower(term_name) LIKE lower(?) OR lower(full_name) LIKE lower(?))"
    )
    params: list = [pattern, pattern]
    if instance_name is not None:
        sql += " AND lower(instance_name) = lower(?)"
        params.append(instance_name)
    db.cursor.execute(sql, params)
    rows = db.cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# SL1 — Embedding + semantic retrieval
# ---------------------------------------------------------------------------

def _build_embed_document(term: dict) -> str:
    """
    Build the text that is embedded into ChromaDB for a term.
    Concatenates term_name, full_name, definition, and synonyms so that
    user queries phrased any of those ways hit this document.
    """
    parts = [str(term.get("term_name") or "").strip()]
    if term.get("full_name"):
        parts.append(str(term["full_name"]).strip())
    if term.get("definition"):
        parts.append(str(term["definition"]).strip())

    synonyms = term.get("synonyms") or []
    if isinstance(synonyms, str):
        try:
            synonyms = json.loads(synonyms)
        except Exception:
            synonyms = []
    if synonyms:
        parts.append("Synonyms: " + ", ".join(str(s) for s in synonyms))

    return ". ".join(p for p in parts if p)


def index_term(term: dict, embedding_model, chroma_client) -> None:
    """
    Embed *term* and upsert it into the 'business_glossary' ChromaDB collection.

    Args:
        term:            dict as returned by get_term() or add_term().
        embedding_model: SentenceTransformer instance (or any object with .encode(str)).
        chroma_client:   Connected ChromaDB client.
    """
    from MetadataManager.MetadataStore.vdb.Chroma import addData

    document = _build_embed_document(term)
    if not document:
        logger.warning("GlossaryStore.index_term: empty document for term id=%s — skipping", term.get("term_id"))
        return

    embedding = embedding_model.encode(document).tolist()
    chroma_id = _chroma_id(term["term_id"])

    data = [
        {
            "documents": document,
            "embedding": embedding,
            "metadata": {
                "term_id":       term["term_id"],
                "term_name":     term.get("term_name", ""),
                "instance_name": term.get("instance_name", ""),
                "domain":        term.get("domain", ""),
            },
            "id": chroma_id,
        }
    ]
    addData(chroma_client, data, {"collection_name": _COLLECTION, "sim_metric": "cosine"})
    _invalidate_retrieval_cache()
    logger.debug("GlossaryStore: indexed term %r into %s", term.get("term_name"), _COLLECTION)


def _chroma_id(term_id: str) -> str:
    return str(uuid.uuid3(uuid.NAMESPACE_DNS, term_id))


def unindex_term(term_id: str, chroma_client) -> None:
    """Remove a term's embedding from the 'business_glossary' collection (no-op if absent)."""
    try:
        collection = chroma_client.get_collection(name=_COLLECTION)
    except Exception:
        return  # nothing indexed yet
    collection.delete(ids=[_chroma_id(term_id)])
    _invalidate_retrieval_cache()


def get_business_context(
    query: str,
    instance_name: str = "default",
    top_k: int = 3,
    max_distance: float = 0.6,
) -> list:
    """
    SL1: Semantic search against the business_glossary ChromaDB collection.

    Returns a list (up to top_k) of matched term dicts ordered by relevance.
    Terms whose cosine distance exceeds max_distance are excluded.
    Falls back to [] on any error (model unavailable, empty collection, etc.).

    Each returned dict is the full term row from SQLite plus a '_distance' key.
    """
    try:
        from Utilities.base_utils import get_config_val
        from MetadataManager.MetadataStore.shared_models import get_chroma_client, get_embedding_model
        from MetadataManager.MetadataStore.vdb.Chroma import getData

        vectordb_cfg = get_config_val("retrieval_config", ["vectordb"], True)

        client = get_chroma_client(vectordb_cfg["path"])
        model  = get_embedding_model()

        query_emb = model.encode(query).tolist()
        vdb_meta  = {"collection_name": _COLLECTION, "n_chunks": top_k}

        extra: dict = {}
        if instance_name and instance_name != "default":
            extra["instance_name"] = instance_name

        raw = getData(client, query_emb, vdb_meta, **extra)

    except Exception as exc:
        logger.warning("GlossaryStore.get_business_context: retrieval failed — %s", exc)
        return []

    ids       = (raw.get("ids")       or [[]])[0]
    metadatas = (raw.get("metadatas") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]

    hits: list = []
    for meta, dist in zip(metadatas, distances):
        if dist > max_distance:
            continue
        db_term = get_term(meta.get("term_id", ""))
        if db_term:
            db_term["_distance"] = dist
            hits.append(db_term)

    hits.sort(key=lambda x: x["_distance"])
    return hits
