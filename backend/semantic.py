"""
backend/semantic.py — semantic-layer interchange (Apache Ossie, formerly OSI)

Routes (all under /api/semantic, authenticated):
  POST /api/semantic/import/ossie — import an Ossie YAML/JSON document:
                                    datasets → tables, relationships → join
                                    edges, metrics → glossary terms
  GET  /api/semantic/export/ossie — export an instance as an Ossie YAML document
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from backend.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/semantic", tags=["semantic"], dependencies=[Depends(get_current_user)])


class OssieImportRequest(BaseModel):
    content: str                   # the Ossie document (YAML or JSON)
    instance_name: str = "default"
    db_type: str = "generic"


@router.post("/import/ossie")
def import_ossie(body: OssieImportRequest):
    """
    Import an Ossie semantic model. Re-importing replaces the same tables'
    metadata and updates metrics by name.

    Returns {models, tables, relationships, metrics, notes, skipped}. A model's
    top-level ai_context is indexed as an OKF note (bundle `ossie:<model>`).
    """
    from MetadataManager.GlossaryStore import index_term
    from MetadataManager.MetadataStore import RAGPipeline
    from MetadataManager.OKFStore import index_concepts
    from MetadataManager.OssieStore import import_into_polyql, load_models

    try:
        load_models(body.content)  # validate before loading any models
        rag = RAGPipeline.ManageInformation()
        rag.initialize_client()
        return import_into_polyql(
            body.content, instance_name=body.instance_name, db_type=body.db_type, vdb=rag,
            glossary_index=lambda term: index_term(term, rag.embedding_model, rag.vdb.client),
            okf_index=lambda notes, bundle: index_concepts(
                notes, rag.embedding_model, rag.vdb.client, body.instance_name, bundle),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Ossie import failed")
        raise HTTPException(status_code=500, detail=f"Ossie import failed: {exc}") from exc


@router.get("/export/ossie", response_class=PlainTextResponse)
def export_ossie(instance_name: str = "default", name: str | None = None):
    """Export an instance's tables, join graph and glossary as Ossie YAML."""
    from MetadataManager.OssieStore import build_document

    try:
        try:
            from backend.okf import _chroma_client
            chroma_client = _chroma_client()
        except Exception:
            logger.warning("Ossie export: ChromaDB unavailable; exporting without ai_context", exc_info=True)
            chroma_client = None
        document = build_document(instance_name, name, chroma_client)
    except Exception as exc:
        logger.exception("Ossie export failed")
        raise HTTPException(status_code=500, detail=f"Ossie export failed: {exc}") from exc
    filename = f"{name or f'polyql_{instance_name}'}.ossie.yaml"
    return PlainTextResponse(document, media_type="application/yaml",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})
