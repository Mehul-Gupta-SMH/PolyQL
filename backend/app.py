import sys
import os
import time
import logging
from contextlib import asynccontextmanager
from typing import Any, Literal, List, Optional
import networkx as nx

# Ensure project root is on the path so `main` can be imported directly
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json as _json

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse, PlainTextResponse
from pydantic import BaseModel

from main import generateQuery, generateQueryStream, gatherRequirements, SQLValidationError, _VALID_PROVIDERS
from APIManager.circuit_breaker import CircuitOpenError, snapshot as _breaker_snapshot
from backend.auth import get_current_user
from backend.logging_config import configure_logging
from backend.metrics import metrics
from backend.rate_limit import rate_limited
from backend.glossary import router as glossary_router
from backend.okf import router as okf_router
from backend.semantic import router as semantic_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    from backend.auth import init_db
    init_db()
    logger.info("Poly-QL backend started", extra={"event": "startup"})
    yield
    logger.info("Poly-QL backend shutting down", extra={"event": "shutdown"})


app = FastAPI(title="SQLCoder API", lifespan=lifespan)

app.include_router(glossary_router)  # SL3: business glossary endpoints
app.include_router(okf_router)       # OKF bundle import for table search
app.include_router(semantic_router)  # Apache Ossie import / export

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:4173"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.exception_handler(CircuitOpenError)
async def _circuit_open_handler(request: Request, exc: CircuitOpenError):
    """A provider's circuit breaker is open: 503 with Retry-After."""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc)},
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


@app.middleware("http")
async def _request_logger(request: Request, call_next):
    """Log every request as a JSON line and record metrics counters."""
    t0 = time.monotonic()
    response = await call_next(request)
    latency_ms = (time.monotonic() - t0) * 1000
    metrics.record_request(request.method, request.url.path, response.status_code, latency_ms)
    logger.info(
        "http request",
        extra={
            "event": "http_request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "latency_ms": round(latency_ms, 2),
        },
    )
    return response


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    query: str
    provider: str
    query_type: Literal["sql", "spark_sql", "dataframe_api", "pandas"] = "sql"
    model: Optional[str] = None
    instance_name: str = "default"


class ExecuteRequest(BaseModel):
    generated_query: str
    query_type: Literal["sql", "spark_sql"]
    connection_string: str
    # Optional context fields for QT1 outcome recording
    nl_query:   Optional[str] = None
    provider:   Optional[str] = None
    session_id: Optional[str] = None
    query_id:   Optional[str] = None


class ChatMessageItem(BaseModel):
    role: str      # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessageItem]
    provider: str
    query_type: Literal["sql", "spark_sql", "dataframe_api", "pandas"] = "sql"
    model: Optional[str] = None
    instance_name: str = "default"


class IngestPreviewRequest(BaseModel):
    sql: str
    provider: str
    model: Optional[str] = None
    instance_name: str = "default"


class ColumnMeta(BaseModel):
    name: str
    type: str = ""
    constraints: str = ""
    desc: str = ""
    source_expr: str = ""   # pipeline lineage — stored in the `logic` field


class RelationshipMeta(BaseModel):
    source: str
    target: str
    join_keys: str = ""
    kind: Literal["fk", "lineage"] = "fk"   # lineage edges are never used for join paths


class IngestCommitRequest(BaseModel):
    table_name: str
    table_desc: str
    columns: List[ColumnMeta]
    relationships: List[RelationshipMeta] = []
    instance_name: str = "default"
    db_type: str = "generic"


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class SessionItem(BaseModel):
    id: str
    title: str
    timestamp: int
    messages: List[Any]
    provider: str
    queryType: str


# ---------------------------------------------------------------------------
# Auth endpoints  (public — no token required)
# ---------------------------------------------------------------------------

@app.post("/auth/register", status_code=201)
def register(body: RegisterRequest):
    from backend.auth import create_user
    try:
        user = create_user(body.username, body.password)
        return {"username": user["username"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/auth/login")
def login(body: LoginRequest):
    from backend.auth import authenticate_user, create_token
    user = authenticate_user(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    return {
        "access_token": create_token(user),
        "token_type": "bearer",
        "username": user["username"],
    }


@app.get("/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {"username": user["sub"], "id": user["uid"]}


@app.get("/auth/google/enabled")
def google_sso_status():
    from backend.auth import google_sso_enabled
    return {"enabled": google_sso_enabled()}


@app.get("/auth/google")
def google_sso_start():
    """Redirect the browser to Google's OAuth2 consent page."""
    from backend.auth import google_sso_enabled, google_auth_url
    if not google_sso_enabled():
        raise HTTPException(status_code=503, detail="Google SSO is not configured.")
    return RedirectResponse(google_auth_url())


@app.get("/auth/google/callback")
def google_sso_callback(code: str = "", error: str = ""):
    """Handle the OAuth2 callback from Google."""
    from backend.auth import google_callback, _frontend_url
    frontend = _frontend_url()

    if error or not code:
        # Redirect to frontend with an error fragment
        return RedirectResponse(f"{frontend}/#sso_error={error or 'missing_code'}")

    try:
        token = google_callback(code)
    except HTTPException as exc:
        return RedirectResponse(f"{frontend}/#sso_error={exc.detail}")

    # Redirect to frontend; the JS picks up the token from the URL hash
    return RedirectResponse(f"{frontend}/#sso_token={token}")


# ---------------------------------------------------------------------------
# Session endpoints  (protected — per-user chat history)
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
def get_sessions(user: dict = Depends(get_current_user)):
    from backend.auth import list_sessions
    return {"sessions": list_sessions(user["uid"])}


@app.post("/api/sessions")
def post_session(body: SessionItem, user: dict = Depends(get_current_user)):
    from backend.auth import upsert_session
    upsert_session(user["uid"], body.model_dump())
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
def delete_session_endpoint(session_id: str, user: dict = Depends(get_current_user)):
    from backend.auth import delete_session
    delete_session(user["uid"], session_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

@app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def get_metrics():
    """Prometheus-compatible metrics endpoint."""
    return PlainTextResponse(metrics.render_prometheus(), media_type="text/plain; version=0.0.4")


@app.get("/api/providers")
def get_providers():
    states = _breaker_snapshot()
    idle = {"state": "closed", "failures": 0, "retry_after": 0.0}
    return {
        "providers": sorted(_VALID_PROVIDERS),
        # additive: circuit-breaker state per provider
        "breakers": {p: states.get(p.lower(), idle) for p in sorted(_VALID_PROVIDERS)},
    }


@app.get("/api/providers/balance")
def get_providers_balance():
    from backend.balance import get_all_balances
    balances = get_all_balances(list(_VALID_PROVIDERS))
    return {"balances": balances}


@app.get("/api/instances")
def get_instances():
    """Return list of distinct instance_name values from tableDesc."""
    from Utilities.base_utils import accessDB
    db = accessDB("table", "tableMetadata")
    try:
        rows = db.get_data("tableDesc", {}, ["instance_name", "db_type"], fetchtype="All") or []
        seen = {}
        for row in rows:
            iname = row[0] or "default"
            dtype = row[1] or "generic"
            if iname not in seen:
                seen[iname] = dtype
        instances = [{"instance_name": k, "db_type": v} for k, v in sorted(seen.items())]
        if not instances:
            instances = [{"instance_name": "default", "db_type": "generic"}]
    except Exception:
        instances = [{"instance_name": "default", "db_type": "generic"}]
    return {"instances": instances}


@app.post("/api/chat")
def post_chat(body: ChatRequest, user: dict = Depends(rate_limited)):
    """
    Two-phase chat endpoint.

    Phase 1 — Requirement Gathering Agent:
      Receives the full conversation history, sees all table names + retrieved schema,
      and either asks a clarifying question or declares it has enough information.

    Phase 2 — Query Generation Agent (only when Phase 1 is ready):
      Uses the requirements summary from Phase 1 as input to generate SQL / Spark SQL / PySpark.
    """
    messages = [m.model_dump() for m in body.messages]

    try:
        gather = gatherRequirements(messages, body.provider, model=body.model, instance_name=body.instance_name)
        metrics.record_llm_call(body.provider)
    except (ValueError, SQLValidationError) as exc:
        metrics.record_llm_call(body.provider, error=True)
        raise HTTPException(status_code=422, detail=str(exc))
    except CircuitOpenError:
        metrics.record_llm_call(body.provider, error=True)
        raise  # -> 503 via _circuit_open_handler
    except Exception as exc:
        metrics.record_llm_call(body.provider, error=True)
        raise HTTPException(status_code=500, detail=f"Requirement gathering failed: {exc}")

    if not gather.get("ready"):
        clarify = {
            "type": "clarify",
            "sql": gather.get("question", "Could you provide more details?"),
            "query_type": body.query_type,
        }
        if gather.get("options"):
            clarify["options"] = gather["options"]
        return clarify

    try:
        result = generateQuery(gather["summary"], body.provider, body.query_type, messages, model=body.model, instance_name=body.instance_name)
        metrics.record_llm_call(body.provider)
    except (ValueError, SQLValidationError) as exc:
        metrics.record_llm_call(body.provider, error=True)
        raise HTTPException(status_code=422, detail=str(exc))
    except CircuitOpenError:
        metrics.record_llm_call(body.provider, error=True)
        raise
    except Exception as exc:
        metrics.record_llm_call(body.provider, error=True)
        raise HTTPException(status_code=500, detail=f"Query generation failed: {exc}")

    return {
        "type": result["type"],
        "sql": result["content"],
        "query_type": body.query_type,
    }


@app.post("/api/chat/stream")
def post_chat_stream(body: ChatRequest, user: dict = Depends(rate_limited)):
    """
    Streaming version of /api/chat.

    Phase 1 (gatherRequirements) runs synchronously — it must complete before we
    know whether to ask a clarifying question or generate a query.

    If Phase 1 needs clarification: emits a single SSE event with type "clarify" then closes.
    If Phase 1 is ready: streams Phase 2 (generateQuery) token-by-token via SSE.

    SSE event shapes:
      data: {"event":"token","data":"<chunk>"}
      data: {"event":"done","type":"sql"|"code"|"clarify","content":"<full text>","query_type":"sql"}
      data: {"event":"error","detail":"<message>"}
    """
    messages = [m.model_dump() for m in body.messages]

    try:
        gather = gatherRequirements(messages, body.provider, model=body.model, instance_name=body.instance_name)
        metrics.record_llm_call(body.provider)
    except (ValueError, SQLValidationError) as exc:
        metrics.record_llm_call(body.provider, error=True)
        raise HTTPException(status_code=422, detail=str(exc))
    except CircuitOpenError:
        metrics.record_llm_call(body.provider, error=True)
        raise  # -> 503 via _circuit_open_handler
    except Exception as exc:
        metrics.record_llm_call(body.provider, error=True)
        raise HTTPException(status_code=500, detail=f"Requirement gathering failed: {exc}")

    def _sse(payload: dict) -> str:
        return f"data: {_json.dumps(payload)}\n\n"

    if not gather.get("ready"):
        clarify_payload = {
            "event": "done",
            "type": "clarify",
            "content": gather.get("question", "Could you provide more details?"),
            "query_type": body.query_type,
        }
        if gather.get("options"):
            clarify_payload["options"] = gather["options"]

        def _clarify_gen():
            yield _sse(clarify_payload)

        return StreamingResponse(_clarify_gen(), media_type="text/event-stream")

    def _stream_gen():
        try:
            for chunk in generateQueryStream(
                gather["summary"],
                body.provider,
                body.query_type,
                messages,
                model=body.model,
                instance_name=body.instance_name,
            ):
                if chunk["event"] == "token":
                    yield _sse({"event": "token", "data": chunk["data"]})
                elif chunk["event"] == "done":
                    yield _sse({
                        "event": "done",
                        "type": chunk["type"],
                        "content": chunk["content"],
                        "query_type": body.query_type,
                    })
            metrics.record_llm_call(body.provider)
        except (ValueError, SQLValidationError) as exc:
            metrics.record_llm_call(body.provider, error=True)
            yield _sse({"event": "error", "detail": str(exc)})
        except CircuitOpenError as exc:
            metrics.record_llm_call(body.provider, error=True)
            yield _sse({"event": "error", "detail": str(exc), "retry_after": exc.retry_after_seconds})
        except Exception as exc:
            metrics.record_llm_call(body.provider, error=True)
            yield _sse({"event": "error", "detail": f"Query generation failed: {exc}"})

    return StreamingResponse(
        _stream_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/query")
def post_query(body: QueryRequest, user: dict = Depends(rate_limited)):
    try:
        result = generateQuery(body.query, body.provider, body.query_type, model=body.model, instance_name=body.instance_name)
        # result = {"type": "sql"|"code"|"clarify", "content": str}
        return {
            "type": result["type"],          # "sql", "code", or "clarify"
            "sql": result["content"],         # keep "sql" key for compat
            "query_type": body.query_type,
        }
    except (ValueError, SQLValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except CircuitOpenError:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/execute")
def post_execute(body: ExecuteRequest, user: dict = Depends(get_current_user)):
    import time
    from backend.executor import execute_query
    from validation.outcome_store import record as record_outcome

    _ctx = dict(
        generated_sql=body.generated_query,
        query_type=body.query_type,
        nl_query=body.nl_query or "",
        provider=body.provider or "",
        session_id=body.session_id or "",
        query_id=body.query_id or "",
    )

    t0 = time.monotonic()
    try:
        columns, rows = execute_query(body.generated_query, body.query_type, body.connection_string)
        latency_ms = (time.monotonic() - t0) * 1000
        outcome = "success" if rows else "empty"
        record_outcome(outcome=outcome, latency_ms=latency_ms, row_count=len(rows), **_ctx)
        return {"columns": columns, "rows": rows}
    except ValueError as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        record_outcome(outcome="failure", latency_ms=latency_ms,
                       error_type="ValueError", error_msg=str(exc), **_ctx)
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        record_outcome(outcome="failure", latency_ms=latency_ms,
                       error_type=type(exc).__name__, error_msg=str(exc), **_ctx)
        raise HTTPException(status_code=500, detail=f"Execution error: {str(exc)}")


@app.get("/api/schema")
def get_schema(instance_name: str = "default"):
    from MetadataManager.MetadataStore.relationdb import kuzuDB
    from MetadataManager.MetadataStore.relationdb.joinpath import (
        FK, LINEAGE, _options, is_join_edge, join_type, load_unique_columns,
    )
    from Utilities.base_utils import accessDB, get_config_val

    graph = kuzuDB.getObj(instance_name)

    tmddb = get_config_val("retrieval_config", ["tableMDdb"], True)
    db = accessDB(tmddb["info_type"], tmddb["dbName"])

    tables = []
    for table in sorted(graph.nodes()):
        desc_lookup = {"tableName": table}
        col_lookup = {"TableName": table}
        if instance_name != "default":
            desc_lookup["instance_name"] = instance_name
            col_lookup["instance_name"] = instance_name
        desc_row = db.get_data(tmddb["tableDescName"], desc_lookup, ["Desc"])
        cols = db.get_data(
            tmddb["tableColName"], col_lookup,
            ["ColumnName", "DataType", "Constraints", "Desc"], fetchtype="All"
        ) or []
        tables.append({
            "name": table,
            "description": desc_row[0] if desc_row else "",
            "columns": [
                {"name": c[0], "type": c[1] or "", "constraints": c[2] or "", "description": c[3] or ""}
                for c in cols
            ],
        })

    # Kuzu stores every join in both directions; draw such a mirror pair once.
    # A reverse edge with different conditions or kind is a distinct relation.
    unique_columns = load_unique_columns(instance_name)
    relations = []
    seen = set()
    for src, tgt, data in graph.edges(data=True):
        options = _options(data)
        kind = LINEAGE if data.get("Kind", FK) == LINEAGE else FK
        if (tgt, src, kind, frozenset(options)) in seen:
            continue
        seen.add((src, tgt, kind, frozenset(options)))
        join = is_join_edge(data)
        relations.append({
            "source": src,
            "target": tgt,
            "joinKeys": options[0] if options else "",
            "alternatives": options,
            "cardinality": join_type(options[0], src, tgt, unique_columns) if join else None,
            "kind": kind,
        })

    return {"tables": tables, "relations": relations}


# ---------------------------------------------------------------------------
# Ingest endpoints
# ---------------------------------------------------------------------------

@app.post("/api/ingest/preview")
def post_ingest_preview(body: IngestPreviewRequest, user: dict = Depends(rate_limited)):
    """
    Parse a data pipeline SQL (INSERT...SELECT or CTAS), look up source table schemas,
    call the LLM to generate the target table data dictionary, and return a preview
    for the user to review before committing.
    """
    from backend.ingestion import (
        parse_pipeline, get_source_schema,
        format_source_schemas, format_column_mappings,
    )
    from main import generate_pipeline_dict

    try:
        parsed = parse_pipeline(body.sql)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    source_schema = get_source_schema(parsed["source_tables"], instance_name=body.instance_name)
    schemas_str = format_source_schemas(source_schema)
    mappings_str = format_column_mappings(parsed["column_mappings"])

    try:
        dd = generate_pipeline_dict(body.sql, schemas_str, mappings_str, body.provider, model=body.model)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM error: {str(exc)}")

    desc_map = {c["name"]: c["desc"] for c in dd.get("columns", [])}
    columns = [
        {
            "name": m["target"],
            "type": "",           # type not available from SELECT — user can fill in
            "constraints": "",
            "desc": desc_map.get(m["target"], ""),
            "source_expr": m["source_expr"],
        }
        for m in parsed["column_mappings"]
    ]

    # Auto-build relationships: each source table → target table
    auto_relationships = [
        {"source": src, "target": parsed["target_table"], "join_keys": "", "kind": "lineage"}
        for src in parsed["source_tables"]
    ]

    return {
        "table_name": parsed["target_table"],
        "table_desc": dd.get("tableDesc", ""),
        "columns": columns,
        "source_tables": parsed["source_tables"],
        "relationships": auto_relationships,
    }


@app.post("/api/ingest/commit")
def post_ingest_commit(body: IngestCommitRequest, user: dict = Depends(get_current_user)):
    """
    Store the reviewed table metadata to SQLite, ChromaDB, and NetworkX.
    """
    from backend.ingestion import store_table

    try:
        store_table(
            table_name=body.table_name,
            table_desc=body.table_desc,
            columns=[c.model_dump() for c in body.columns],
            relationships=[r.model_dump() for r in body.relationships],
            instance_name=body.instance_name,
            db_type=body.db_type,
        )
        return {"success": True, "table": body.table_name}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Commit error: {str(exc)}")


# ---------------------------------------------------------------------------
# Lineage endpoint
# ---------------------------------------------------------------------------

@app.get("/api/lineage/{table_name}")
def get_lineage(table_name: str, instance_name: str = "default"):
    """
    Return the lineage subgraph connected to *table_name* from the NetworkX graph.
    The connected component is returned so the UI can render the actual lineage flow.
    """
    from MetadataManager.MetadataStore.relationdb import kuzuDB

    graph = kuzuDB.getObj(instance_name)
    name = table_name.lower()

    if name not in graph.nodes():
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found in schema.")

    component = nx.node_connected_component(graph.to_undirected(), name)

    nodes = [{"id": name, "role": "center"}]
    nodes.extend({"id": n, "role": "related"} for n in sorted(component - {name}))

    # Deduplicate edges (graph is bidirectional — each pair stored in both directions)
    seen_pairs = set()
    edges = []
    for src, tgt, data in graph.edges(data=True):
        if src not in component or tgt not in component:
            continue
        edge_key = frozenset({src, tgt})
        if edge_key in seen_pairs:
            continue
        seen_pairs.add(edge_key)
        edges.append({
            "source": src,
            "target": tgt,
            "joinKeys": data.get("JoinKeys", ""),
            "joinType": _determine_join_type(
                data.get("JoinKeys", ""), src, tgt
            ),
        })

    return {"center": name, "nodes": nodes, "edges": edges}


@app.get("/api/joinpath")
def get_join_path(from_table: str, to_table: str, instance_name: str = "default"):
    """
    Return the best JOIN path between two tables.

    Uses only join edges (not pipeline lineage), prefers key-to-key hops over
    many-to-many, and lists every alternative join condition for each hop.
    """
    from MetadataManager.MetadataStore.relationdb import kuzuDB
    from MetadataManager.MetadataStore.relationdb.joinpath import connect_tables, load_unique_columns

    graph = kuzuDB.getObj(instance_name)
    src = from_table.lower()
    tgt = to_table.lower()

    if src not in graph.nodes():
        raise HTTPException(status_code=404, detail=f"Table '{from_table}' not found.")
    if tgt not in graph.nodes():
        raise HTTPException(status_code=404, detail=f"Table '{to_table}' not found.")
    if src == tgt:
        return {"found": True, "path": [src], "edges": []}

    relations, unreachable = connect_tables(graph, [src, tgt], load_unique_columns(instance_name))
    if unreachable or not relations:
        return {"found": False, "path": [], "edges": []}

    path = [src]
    for rel in relations:
        path.append(rel["target"])
    edges = [
        {
            "source": rel["source"],
            "target": rel["target"],
            "joinKeys": rel["join_keys"],
            "joinType": rel["cardinality"],
            "alternatives": rel["alternatives"],
        }
        for rel in relations
    ]
    return {"found": True, "path": path, "edges": edges}


@app.get("/api/derivatives/{table_name}")
def get_derivatives(table_name: str, instance_name: str = "default"):
    """
    Return tables that are pipeline-derived FROM the given table,
    and whether the given table itself is a derivative.
    """
    from MetadataManager.MetadataStore.relationdb import kuzuDB
    from Utilities.base_utils import accessDB, get_config_val

    graph = kuzuDB.getObj(instance_name)
    tmddb = get_config_val("retrieval_config", ["tableMDdb"], True)
    db = accessDB(tmddb["info_type"], tmddb["dbName"])

    name = table_name.lower()

    # Tables that have pipeline-type columns are derived tables
    def _is_derived(tbl):
        rows = db.get_data(
            tmddb["tableColName"], {"TableName": tbl, "type_of_logic": "pipeline"},
            ["ColumnName"], fetchtype="All",
        ) or []
        return len(rows) > 0

    # Tables reachable from `name` in the directed graph that are derived tables
    derived = []
    if name in graph.nodes():
        for neighbour in graph.successors(name):
            if _is_derived(neighbour):
                desc_row = db.get_data(tmddb["tableDescName"], {"tableName": neighbour}, ["Desc"])
                derived.append({
                    "name": neighbour,
                    "description": (desc_row[0] if desc_row else "") or "",
                })

    # Also find any table with pipeline columns that lists `name` as a source (via graph predecessors)
    parent_tables = []
    if _is_derived(name) and name in graph.nodes():
        for pred in graph.predecessors(name):
            if not _is_derived(pred):  # skip other derived tables
                desc_row = db.get_data(tmddb["tableDescName"], {"tableName": pred}, ["Desc"])
                parent_tables.append({
                    "name": pred,
                    "description": (desc_row[0] if desc_row else "") or "",
                })

    return {
        "table": name,
        "is_derived": _is_derived(name),
        "derived_tables": derived,
        "parent_tables": parent_tables,
    }


def _determine_join_type(join_keys, src, tgt):
    """Cardinality of joining src → tgt on join_keys (1:1, 1:n, n:1, n:m)."""
    from MetadataManager.MetadataStore.relationdb.joinpath import join_type, load_unique_columns

    if not join_keys:
        return "n:m"
    return join_type(join_keys, src, tgt, load_unique_columns())
