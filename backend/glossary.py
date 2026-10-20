"""
backend/glossary.py — SL3: Business Glossary REST API

Routes (all under /api/glossary, authenticated):
  POST   /api/glossary/terms          — add one term or a list of terms
  POST   /api/glossary/terms/single   — add one term (409 if the name exists in the instance)
  POST   /api/glossary/terms/bulk     — add a list of terms (?upsert=true updates by name)
  GET    /api/glossary/terms          — list terms (?instance_name=&domain=)
  GET    /api/glossary/terms/{id}     — get term by ID
  PUT    /api/glossary/terms/{id}     — update term fields
  DELETE /api/glossary/terms/{id}     — delete term
  GET    /api/glossary/search         — lexical search (?q=&instance_name=)
  POST   /api/glossary/reindex        — re-embed every term (?instance_name=)

Every write also updates the term's embedding in the 'business_glossary'
ChromaDB collection, which is what semantic search (and so the prompt's
Business Definitions) reads. Embedding is best effort: the term is saved even
when the model can't load, and the response says "indexed": false.
"""

import functools
import logging
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/glossary", tags=["glossary"], dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class TermIn(BaseModel):
    term_id:       Optional[str]       = None
    term_name:     str
    full_name:     Optional[str]       = None
    definition:    Optional[str]       = None
    formula:       Optional[str]       = None
    formula_type:  Optional[str]       = None
    table_deps:    Optional[List[str]] = None
    column_deps:   Optional[List[str]] = None
    synonyms:      Optional[List[str]] = None
    example_value: Optional[str]       = None
    domain:        Optional[str]       = None
    instance_name: Optional[str]       = ""


class TermUpdate(BaseModel):
    term_name:     Optional[str]       = None
    full_name:     Optional[str]       = None
    definition:    Optional[str]       = None
    formula:       Optional[str]       = None
    formula_type:  Optional[str]       = None
    table_deps:    Optional[List[str]] = None
    column_deps:   Optional[List[str]] = None
    synonyms:      Optional[List[str]] = None
    example_value: Optional[str]       = None
    domain:        Optional[str]       = None
    instance_name: Optional[str]       = None


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _rag():
    """Embedding model + ChromaDB client, loaded once per process."""
    from MetadataManager.MetadataStore import RAGPipeline

    rag = RAGPipeline.ManageInformation()
    rag.initialize_client()
    return rag


def _index(term_ids: list) -> bool:
    """Embed the given terms; False (logged) when the model or vector store is unavailable."""
    from MetadataManager.GlossaryStore import get_term, index_term

    try:
        rag = _rag()
        for term_id in term_ids:
            term = get_term(term_id)
            if term:
                index_term(term, rag.embedding_model, rag.vdb.client)
        return True
    except Exception as exc:
        logger.warning("Glossary: embedding failed, terms saved but not searchable yet — %s", exc)
        return False


def _check_names(items: List[TermIn]) -> None:
    """Reject the whole request before writing anything if a term has no name."""
    for i, item in enumerate(items):
        if not item.term_name.strip():
            raise HTTPException(status_code=422, detail=f"Term {i + 1}: term_name is required")


def _find(term_name: str, instance_name: str) -> Optional[dict]:
    """The term with this name (case-insensitive) in exactly this instance."""
    from MetadataManager.GlossaryStore import list_terms

    name = term_name.strip().lower()
    return next((t for t in list_terms(instance_name=instance_name or "")
                 if (t.get("term_name") or "").strip().lower() == name), None)


def _add(items: List[TermIn], upsert: bool = False) -> dict:
    from MetadataManager.GlossaryStore import add_term, update_term

    _check_names(items)
    created, updated = [], []
    for item in items:
        data = item.model_dump()
        existing = _find(item.term_name, item.instance_name) if upsert else None
        if existing:
            fields = {k: v for k, v in data.items() if v is not None and k not in ("term_id", "term_name")}
            update_term(existing["term_id"], fields)
            updated.append(existing["term_id"])
        else:
            created.append(add_term(data))
    return {"created": created, "updated": updated, "indexed": _index(created + updated)}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/terms", status_code=201)
def create_terms(body: Union[List[TermIn], TermIn]):
    """
    Add one or more business terms.

    Accepts either a single TermIn object or a list of TermIn objects.
    Returns {created: [term_id, ...], updated: [], indexed}.
    """
    return _add(body if isinstance(body, list) else [body])


@router.post("/terms/bulk", status_code=201)
def create_terms_bulk(body: List[TermIn], upsert: bool = False):
    """
    Bulk-insert business terms. With upsert=true a term whose name already
    exists in the same instance is updated instead of duplicated, so the same
    CSV/JSON file can be imported again.
    """
    return _add(body, upsert=upsert)


@router.post("/terms/single", status_code=201)
def create_term_single(body: TermIn):
    """Add a single business term. Returns the created term_id; 409 if the name exists in the instance."""
    _check_names([body])
    if _find(body.term_name, body.instance_name):
        raise HTTPException(status_code=409, detail=f"Term '{body.term_name}' already exists.")
    result = _add([body])
    return {"term_id": result["created"][0], "indexed": result["indexed"]}


@router.get("/terms")
def list_terms_endpoint(instance_name: Optional[str] = None, domain: Optional[str] = None):
    """
    List all business terms, optionally filtered by instance_name and/or domain.
    """
    from MetadataManager.GlossaryStore import list_terms
    terms = list_terms(instance_name=instance_name, domain=domain)
    return {"terms": terms}


@router.get("/terms/{term_id}")
def get_term_endpoint(term_id: str):
    """Fetch a single business term by ID."""
    from MetadataManager.GlossaryStore import get_term
    term = get_term(term_id)
    if term is None:
        raise HTTPException(status_code=404, detail=f"Term '{term_id}' not found.")
    return term


@router.put("/terms/{term_id}")
def update_term_endpoint(term_id: str, body: TermUpdate):
    """Update fields on an existing business term and re-embed it."""
    from MetadataManager.GlossaryStore import get_term, update_term
    current = get_term(term_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Term '{term_id}' not found.")
    if body.term_name is not None:
        if not body.term_name.strip():
            raise HTTPException(status_code=422, detail="term_name must not be empty")
        instance = body.instance_name if body.instance_name is not None else current.get("instance_name")
        clash = _find(body.term_name, instance)
        if clash and clash["term_id"] != term_id:
            raise HTTPException(status_code=409, detail=f"Term '{body.term_name}' already exists.")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    update_term(term_id, updates)
    return {"ok": True, "term_id": term_id, "indexed": _index([term_id])}


@router.delete("/terms/{term_id}", status_code=200)
def delete_term_endpoint(term_id: str):
    """Delete a business term by ID, including its embedding."""
    from MetadataManager.GlossaryStore import delete_term, get_term, unindex_term
    if get_term(term_id) is None:
        raise HTTPException(status_code=404, detail=f"Term '{term_id}' not found.")
    delete_term(term_id)
    try:
        unindex_term(term_id, _rag().vdb.client)
    except Exception as exc:
        # Search skips ids missing from SQLite, so a leftover embedding is harmless
        logger.warning("Glossary: could not remove embedding for %s — %s", term_id, exc)
    return {"ok": True}


@router.get("/search")
def search_terms_endpoint(q: str, instance_name: Optional[str] = None):
    """Lexical search on term_name and full_name. Returns matching terms."""
    from MetadataManager.GlossaryStore import search_by_name
    if not q or not q.strip():
        raise HTTPException(status_code=422, detail="Query parameter 'q' must not be empty.")
    results = search_by_name(q.strip(), instance_name=instance_name)
    return {"results": results}


@router.post("/reindex")
def reindex_terms(instance_name: Optional[str] = None):
    """
    Re-embed every term (or one instance's). Use after a model change, or for
    terms added before the API embedded them.
    """
    from MetadataManager.GlossaryStore import list_terms

    ids = [t["term_id"] for t in list_terms(instance_name=instance_name)]
    if ids and not _index(ids):
        raise HTTPException(status_code=503, detail="Embedding model or vector store unavailable.")
    return {"indexed": len(ids)}
