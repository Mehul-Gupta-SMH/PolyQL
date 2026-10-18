"""
backend/okf.py — Open Knowledge Format (OKF) bundle import

Routes (all under /api/okf, authenticated):
  POST /api/okf/import — embed an OKF bundle directory on the server into the
                         'okf_concepts' collection used by table search
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/okf", tags=["okf"], dependencies=[Depends(get_current_user)])


class OKFImportRequest(BaseModel):
    path: str                     # bundle directory on the server
    instance_name: str = "default"


@router.post("/import")
def import_okf_bundle(body: OKFImportRequest):
    """
    Import an OKF bundle so its concepts take part in table search.

    Returns {indexed, skipped, deprecated, table_concepts}.
    """
    from MetadataManager.MetadataStore import RAGPipeline
    from MetadataManager.OKFStore import import_bundle

    try:
        rag = RAGPipeline.ManageInformation()
        rag.initialize_client()
        return import_bundle(
            body.path, rag.embedding_model, rag.vdb.client, instance_name=body.instance_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("OKF import failed for %s", body.path)
        raise HTTPException(status_code=500, detail=f"OKF import failed: {exc}") from exc
