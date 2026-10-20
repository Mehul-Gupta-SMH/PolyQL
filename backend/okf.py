"""
backend/okf.py — Open Knowledge Format (OKF) bundle import

Routes (all under /api/okf, authenticated):
  POST   /api/okf/import  — embed an OKF bundle directory on the server into the
                            'okf_concepts' collection used by table search
  GET    /api/okf/bundles — imported bundles per instance, with concept counts
  DELETE /api/okf/bundles — remove an instance's concepts (one bundle, or all)
  GET    /api/okf/export  — an instance's data dictionaries + glossary as an
                            OKF bundle (zip)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from backend.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/okf", tags=["okf"], dependencies=[Depends(get_current_user)])


class OKFImportRequest(BaseModel):
    path: str                     # bundle directory on the server
    instance_name: str = "default"
    bundle_name: str | None = None  # defaults to the directory name


def _chroma_client():
    """ChromaDB client only — listing/removing bundles needs no embedding model."""
    from MetadataManager.MetadataStore.vdb.Chroma import getclient
    from Utilities.base_utils import get_config_val

    vectordb = get_config_val("retrieval_config", ["vectordb"], True)
    return getclient({"path": vectordb["path"]}, session_type="local")


@router.post("/import")
def import_okf_bundle(body: OKFImportRequest):
    """
    Import an OKF bundle so its concepts take part in table search.

    Returns {bundle, indexed, skipped, deprecated, table_concepts}.
    """
    from MetadataManager.MetadataStore import RAGPipeline
    from MetadataManager.OKFStore import import_bundle

    try:
        rag = RAGPipeline.ManageInformation()
        rag.initialize_client()
        return import_bundle(
            body.path, rag.embedding_model, rag.vdb.client, instance_name=body.instance_name,
            bundle_name=body.bundle_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("OKF import failed for %s", body.path)
        raise HTTPException(status_code=500, detail=f"OKF import failed: {exc}") from exc


@router.get("/bundles")
def list_okf_bundles():
    """Imported bundles: [{instance_name, bundle, concepts}]."""
    from MetadataManager.OKFStore import list_bundles

    try:
        return {"bundles": list_bundles(_chroma_client())}
    except Exception as exc:
        logger.exception("Listing OKF bundles failed")
        raise HTTPException(status_code=500, detail=f"Listing OKF bundles failed: {exc}") from exc


@router.delete("/bundles")
def delete_okf_bundle(instance_name: str, bundle: str | None = None):
    """Remove one imported bundle (or every bundle of the instance when bundle is omitted)."""
    from MetadataManager.OKFStore import delete_bundle

    try:
        return {"removed": delete_bundle(_chroma_client(), instance_name, bundle)}
    except Exception as exc:
        logger.exception("Removing OKF bundle failed")
        raise HTTPException(status_code=500, detail=f"Removing OKF bundle failed: {exc}") from exc


@router.get("/export")
def export_okf_bundle(instance_name: str = "default"):
    """Download the instance's tables and glossary as an OKF v0.2 bundle (zip)."""
    import io
    import zipfile
    from datetime import datetime, timezone

    from MetadataManager.OKFStore import export_bundle
    from MetadataManager.OssieStore import read_instance

    try:
        tables, edges, unique_columns, terms = read_instance(instance_name)
        files = export_bundle(tables, edges, unique_columns, terms,
                              datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception as exc:
        logger.exception("OKF export failed")
        raise HTTPException(status_code=500, detail=f"OKF export failed: {exc}") from exc

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, text in sorted(files.items()):
            archive.writestr(f"polyql_{instance_name}/{path}", text)
    return Response(buffer.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="polyql_{instance_name}.okf.zip"',
    })
