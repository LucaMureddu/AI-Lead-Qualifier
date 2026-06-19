"""
api/routes.py
-------------
FastAPI routers — V2.1.

V2 changes vs V1
----------------
- SSE (StreamingResponse) REMOVED from lead qualification
- New async pattern: POST /lead → 202 (enqueue ARQ job) → GET /status/{id} polling
- HITL: POST /lead/{thread_id}/approve (replaces SSE interrupt)
- JWT: uses api/dependencies.py (RS256) instead of api/security.py (HS256)
- Admin wipe: uses database.vector_store.wipe_tenant (pgvector) instead of ChromaDB
- Catalogue ingestion: kept (ingest_router) — still SSE internally via ARQ task
- Upload + Profile routers: unchanged logic, updated auth import

V2.1 changes vs V2
------------------
- Rate limiting (slowapi): @limiter.limit applicato a POST /lead e POST /token.
  Il limiter è il singleton di core/rate_limit.py (backend Redis, fail-open).
  request: Request aggiunto come primo parametro nelle funzioni limitate.
- Upload S3: upload_catalogue scrive su S3 via services/storage.upload_file()
  invece di scrivere su filesystem locale. UploadResponse restituisce object_key
  (S3 Object Key) al posto di file_path (path assoluto su disco).
  IngestRequest usa object_key al posto di file_path.
- Rimossi: dipendenza da Path, UPLOAD_DIR, volumi Docker per gli upload.

Singletons from app.state
-------------------------
Route handlers access shared resources via lightweight FastAPI dependencies:
- get_redis()           → app.state.redis           (ArqRedis pool)
- get_graph()           → app.state.graph            (compiled qualification graph)
- get_ingestion_graph() → app.state.ingestion_graph (compiled ingestion graph)
All three are initialised once in main.py lifespan, never rebuilt per-request.

Routers
-------
auth_router    — POST /token (dev only, disabled by TOKEN_ENDPOINT_ENABLED=false)
router         — POST /lead, GET /status/{id}, POST /lead/{id}/approve
ingest_router  — POST /ingest/stream, POST /ingest/{id}/approve
upload_router  — POST /upload
profile_router — GET/PUT /tenants/{id}/profile
admin_router   — DELETE /api/v1/tenants/{id}/vector-data
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Literal, Optional

import structlog
from arq import ArqRedis
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from pydantic import BaseModel, Field

from api.dependencies import create_access_token, get_current_tenant_id
from core.config import get_settings
from core.rate_limit import limiter
from core.state import AgentState, LeadContext
from database.profiles import get_profile, upsert_profile
from database.vector_store import wipe_tenant
from ingestion.graph import make_initial_state
from services.storage import upload_file

log = structlog.get_logger()

# ── MIME type maps for S3 uploads ─────────────────────────────────────────────
_ALLOWED_EXTENSIONS: Dict[str, str] = {".csv": "csv", ".json": "json", ".xlsx": "xlsx"}

_CONTENT_TYPE_MAP: Dict[str, str] = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
}


# ── Shared-resource dependencies ──────────────────────────────────────────────
# These read from app.state, which is populated once in main.py lifespan.
# No new connections are opened per request.

def get_redis(request: Request) -> ArqRedis:
    """Return the shared ARQ Redis pool from app.state."""
    return request.app.state.redis


def get_graph(request: Request):
    """Return the compiled qualification graph from app.state."""
    return request.app.state.graph


def get_ingestion_graph(request: Request):
    """Return the compiled ingestion graph from app.state."""
    return request.app.state.ingestion_graph


# ── Routers ───────────────────────────────────────────────────────────────────

router: APIRouter = APIRouter(tags=["lead-qualification"])
ingest_router: APIRouter = APIRouter(prefix="/ingest", tags=["catalogue-ingestion"])
upload_router: APIRouter = APIRouter(tags=["catalogue-upload"])
auth_router: APIRouter = APIRouter(tags=["auth"])
profile_router: APIRouter = APIRouter(tags=["tenant-profile"])
admin_router: APIRouter = APIRouter(prefix="/api/v1/tenants", tags=["admin"])


# ═════════════════════════════════════════════════════════════════════════════
# LEAD QUALIFICATION  (V2 — async 202 + polling, no SSE)
# ═════════════════════════════════════════════════════════════════════════════

class LeadRequest(BaseModel):
    """Inbound lead payload."""
    raw_text: str = Field(..., min_length=10)
    lead_id: Optional[str] = Field(
        default=None,
        description="Optional external lead identifier. Auto-generated if omitted.",
    )


class LeadAcceptedResponse(BaseModel):
    """202 response: job enqueued."""
    thread_id: str
    status: str = "queued"


class LeadStatusResponse(BaseModel):
    """Polling response from GET /status/{thread_id}."""
    thread_id: str
    status: str  # queued|processing|pending_review|completed|error
    result: Optional[Dict[str, Any]] = None
    error_detail: Optional[str] = None


class HITLDecision(BaseModel):
    """Human reviewer's approval/rejection decision."""
    approved: bool
    feedback: Optional[str] = None


class HITLResponse(BaseModel):
    """Response after /approve."""
    thread_id: str
    status: str  # "resumed" | "rejected"


@router.post("/lead", status_code=202, response_model=LeadAcceptedResponse)
@limiter.limit(get_settings().rate_limit_lead)
async def ingest_lead(
    request: Request,
    body: LeadRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    redis: ArqRedis = Depends(get_redis),
) -> LeadAcceptedResponse:
    """
    Enqueue a lead qualification job and return 202 immediately.

    The client polls GET /status/{thread_id} to follow progress.

    Rate limited: see ``settings.rate_limit_lead`` (default: 5/minute per IP).
    """
    lead_id: str = body.lead_id or str(uuid.uuid4())
    thread_id: str = f"qualify-{tenant_id}-{lead_id}"
    lead_context = LeadContext(
        lead_id=lead_id,
        tenant_id=tenant_id,
        raw_payload={"text": body.raw_text},
    )

    await redis.enqueue_job(
        "run_qualification_task",
        thread_id=thread_id,
        lead_context_dict=lead_context.model_dump(),
        tenant_id=tenant_id,
    )

    log.info("lead.enqueued", lead_id=lead_id, tenant_id=tenant_id)
    return LeadAcceptedResponse(thread_id=thread_id)


@router.get("/status/{thread_id}", response_model=LeadStatusResponse)
async def get_lead_status(
    thread_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    graph=Depends(get_graph),
) -> LeadStatusResponse:
    """
    Poll the status of a lead qualification job.

    Reads the LangGraph checkpoint from Postgres (written by the ARQ worker).
    Returns the current status and, when completed, the result payload.
    """
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(config)

    if not snapshot or not snapshot.values:
        # The ARQ worker hasn't written its first checkpoint yet.
        return LeadStatusResponse(thread_id=thread_id, status="queued")

    state: AgentState = snapshot.values

    # Security: verify tenant ownership before returning any data
    if state["lead"].tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Accesso negato.")

    result: Optional[Dict[str, Any]] = None
    current_status: str = state.get("status", "processing")

    if current_status == "completed":
        result = {
            "total_quote": state.get("total_quote"),
            "mapped_services": state.get("mapped_services"),
            "on_request_services": state.get("on_request_services"),
        }
    elif current_status == "pending_review":
        result = {
            "review_payload": {
                "confidence_score": state.get("confidence_score"),
                "extracted_services": state.get("extracted_services"),
                "mapped_services": state.get("mapped_services"),
            }
        }

    return LeadStatusResponse(
        thread_id=thread_id,
        status=current_status,
        result=result,
        error_detail=state.get("error_detail"),
    )


@router.post("/lead/{thread_id}/approve", response_model=HITLResponse)
async def approve_lead(
    thread_id: str,
    body: HITLDecision,
    tenant_id: str = Depends(get_current_tenant_id),
    redis: ArqRedis = Depends(get_redis),
    graph=Depends(get_graph),
) -> HITLResponse:
    """
    Approve or reject a lead job in pending_review status.

    Flow:
    1. Read the Postgres checkpoint and verify tenant ownership.
    2. Write human_approved + review_feedback into AgentState.
    3. If approved: re-enqueue on ARQ (worker resumes from post-interrupt node).
    4. If rejected: mark as error and terminate.
    """
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(config)

    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail=f"Job {thread_id!r} non trovato.")

    state: AgentState = snapshot.values
    if state["lead"].tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Accesso negato.")
    if state.get("status") != "pending_review":
        raise HTTPException(
            status_code=409,
            detail=f"Job non in pending_review (stato: {state.get('status')}).",
        )

    if not body.approved:
        await graph.aupdate_state(
            config,
            {
                "human_approved": False,
                "review_feedback": body.feedback,
                "status": "error",
                "error_detail": "Rifiutato dall'operatore.",
            },
        )
        return HITLResponse(thread_id=thread_id, status="rejected")

    await graph.aupdate_state(
        config,
        {
            "human_approved": True,
            "review_feedback": body.feedback,
            "status": "queued",
            "confidence_score": 1.0,  # human approval = max confidence
        },
    )
    await redis.enqueue_job(
        "run_qualification_task_resume",
        thread_id=thread_id,
        tenant_id=tenant_id,
    )
    return HITLResponse(thread_id=thread_id, status="resumed")


# ═════════════════════════════════════════════════════════════════════════════
# CATALOGUE INGESTION  (SSE kept — ingestion uses its own graph + interrupt flow)
# ═════════════════════════════════════════════════════════════════════════════

class IngestRequest(BaseModel):
    # V2.1: object_key (S3) sostituisce file_path (path assoluto su disco).
    # Valore proveniente da UploadResponse.object_key dopo il caricamento su S3.
    object_key: str = Field(
        ...,
        description="S3 Object Key del file catalogo (da UploadResponse.object_key).",
    )
    file_format: Literal["csv", "json", "xlsx"]
    review_feedback: Optional[str] = None


class ApprovalDecision(BaseModel):
    approved: bool
    feedback: Optional[str] = None


class ApprovalResponse(BaseModel):
    thread_id: str
    status: Literal["completed", "rejected"]
    total_items: int
    flagged_count: int
    validation_errors: list[str]
    error: Optional[str] = None


def _format_sse(data: str, event: Optional[str] = None) -> str:
    lines: list[str] = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {data}")
    lines.append("")
    return "\n".join(lines) + "\n"


async def _ingest_sse_generator(
    graph,
    tenant_id: str,
    object_key: str,
    file_format: Literal["csv", "json", "xlsx"],
    thread_id: str,
    review_feedback: Optional[str],
):
    """
    SSE generator for catalogue ingestion.

    V2.1: riceve object_key (S3) invece di file_path (filesystem).
    make_initial_state è responsabile di scaricare il file da S3 se necessario.
    """
    initial_state = make_initial_state(
        tenant_id, object_key, file_format, review_feedback
    )
    config: Dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    emitted_log_count: int = 0
    final_snapshot = initial_state

    try:
        async for snapshot in graph.astream(
            initial_state, config=config, stream_mode="values"
        ):
            final_snapshot = snapshot
            all_logs: list[str] = snapshot.get("sse_logs", [])
            new_logs = all_logs[emitted_log_count:]
            for log_line in new_logs:
                yield _format_sse(log_line, event="log")
            emitted_log_count += len(new_logs)

    except GraphInterrupt as gi:
        interrupts = gi.args[0] if gi.args else ()
        review_payload: Any = interrupts[0].value if interrupts else {}
        log.warning("ingest.interrupted", tenant_id=tenant_id, thread_id=thread_id)
        yield _format_sse(
            json.dumps({
                "thread_id": thread_id,
                "tenant_id": tenant_id,
                "review_payload": review_payload,
            }),
            event="interrupt",
        )
        return

    except Exception as exc:
        log.exception(
            "ingest.error",
            tenant_id=tenant_id,
            thread_id=thread_id,
            error=str(exc),
        )
        yield _format_sse(
            json.dumps({
                "error": str(exc),
                "tenant_id": tenant_id,
                "thread_id": thread_id,
            }),
            event="error",
        )
        return

    # LangGraph 1.x: check for pending interrupts in checkpoint
    try:
        snapshot_state = await graph.aget_state(config)
        pending = list(getattr(snapshot_state, "interrupts", ()) or ())
        if not pending:
            for task in getattr(snapshot_state, "tasks", ()) or ():
                pending.extend(getattr(task, "interrupts", ()) or ())
    except Exception:
        pending = []

    if pending:
        first = pending[0]
        review_payload = getattr(first, "value", first)
        yield _format_sse(
            json.dumps({
                "thread_id": thread_id,
                "tenant_id": tenant_id,
                "review_payload": review_payload,
            }),
            event="interrupt",
        )
        return

    yield _format_sse(
        json.dumps({
            "thread_id": thread_id,
            "tenant_id": tenant_id,
            "total_items": len(final_snapshot.get("normalized_items", [])),
            "flagged_count": len(final_snapshot.get("flagged_items", [])),
            "validation_errors": final_snapshot.get("validation_errors", []),
            "error": final_snapshot.get("error"),
        }),
        event="done",
    )


@ingest_router.post("/stream", summary="Ingest a service catalogue via SSE")
async def ingest_stream(
    request_body: IngestRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    graph=Depends(get_ingestion_graph),
) -> StreamingResponse:
    thread_id: str = f"ingest-{tenant_id}-{uuid.uuid4()}"
    log.info(
        "ingest.started",
        tenant_id=tenant_id,
        thread_id=thread_id,
        object_key=request_body.object_key,
    )
    return StreamingResponse(
        _ingest_sse_generator(
            graph=graph,
            tenant_id=tenant_id,
            object_key=request_body.object_key,
            file_format=request_body.file_format,
            thread_id=thread_id,
            review_feedback=request_body.review_feedback,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Thread-Id": thread_id,
        },
    )


@ingest_router.post(
    "/{thread_id}/approve",
    response_model=ApprovalResponse,
    summary="Resume a suspended ingestion run",
)
async def approve_ingestion(
    thread_id: str,
    body: ApprovalDecision,
    tenant_id: str = Depends(get_current_tenant_id),
    graph=Depends(get_ingestion_graph),
) -> ApprovalResponse:
    config: Dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    # Security: verify the thread belongs to this tenant before resuming
    snapshot = await graph.aget_state(config)
    if not snapshot or not snapshot.values:
        raise HTTPException(
            status_code=404,
            detail=f"Nessuna run trovata per thread_id='{thread_id}'.",
        )
    if snapshot.values.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Accesso negato.")

    try:
        final_state = await graph.ainvoke(
            Command(resume={"approved": body.approved, "feedback": body.feedback or ""}),
            config=config,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Nessuna run sospesa per thread_id='{thread_id}'.",
        ) from exc
    except Exception as exc:
        log.exception("ingest.approval_failed", thread_id=thread_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ApprovalResponse(
        thread_id=thread_id,
        status="completed" if body.approved else "rejected",
        total_items=len(final_state.get("normalized_items", [])),
        flagged_count=len(final_state.get("flagged_items", [])),
        validation_errors=final_state.get("validation_errors", []),
        error=final_state.get("error"),
    )


# ═════════════════════════════════════════════════════════════════════════════
# CATALOGUE UPLOAD — V2.1: S3 via aioboto3 (services/storage.py)
# ═════════════════════════════════════════════════════════════════════════════

class UploadResponse(BaseModel):
    # V2.1: object_key (S3) sostituisce file_path (filesystem locale).
    # Da passare a IngestRequest.object_key per avviare l'ingestion.
    object_key: str = Field(
        ...,
        description=(
            "S3 Object Key del file caricato. "
            "Passare a POST /ingest/stream come object_key."
        ),
    )
    file_format: Literal["csv", "json", "xlsx"]


@upload_router.post("/upload", response_model=UploadResponse, tags=["catalogue-upload"])
async def upload_catalogue(
    file: UploadFile = File(...),
    tenant_id: str = Depends(get_current_tenant_id),
) -> UploadResponse:
    """
    Upload a service catalogue file (CSV / JSON / XLSX) to S3.

    Returns the S3 Object Key and the detected file format.
    Pass ``object_key`` to ``POST /ingest/stream`` to start ingestion.

    Security
    --------
    - Il nome originale del file non entra mai nella S3 Object Key (no path traversal).
    - Object Key format: ``<safe_tenant_id>/<uuid_hex><.ext>``  (vedi services/storage.py).
    - La dimensione è validata prima dell'upload (max: settings.upload_max_bytes).
    """
    settings = get_settings()

    # Extension detection — split on last dot to handle names like "catalogue.v2.csv"
    original_name: str = file.filename or ""
    dot_idx: int = original_name.rfind(".")
    ext_str: str = f".{original_name[dot_idx + 1:].lower()}" if dot_idx >= 0 else ""

    if ext_str not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Estensione non supportata: '{ext_str}'. Usa .csv, .json o .xlsx.",
        )

    file_format: str = _ALLOWED_EXTENSIONS[ext_str]
    content_type: str = _CONTENT_TYPE_MAP[ext_str]

    contents: bytes = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="File vuoto.")
    if len(contents) > settings.upload_max_bytes:
        raise HTTPException(status_code=413, detail="File troppo grande.")

    try:
        object_key: str = await upload_file(
            contents=contents,
            content_type=content_type,
            tenant_id=tenant_id,
            extension=ext_str,
        )
    except (ClientError, BotoCoreError) as exc:
        log.error("upload.s3_error", tenant_id=tenant_id, error=str(exc))
        raise HTTPException(
            status_code=502,
            detail="Impossibile caricare il file su S3. Riprovare tra qualche istante.",
        ) from exc
    except Exception as exc:
        log.exception("upload.unexpected_error", tenant_id=tenant_id)
        raise HTTPException(
            status_code=500, detail="Errore interno durante l'upload."
        ) from exc

    log.info(
        "upload.complete",
        tenant_id=tenant_id,
        object_key=object_key,
        format=file_format,
    )
    return UploadResponse(object_key=object_key, file_format=file_format)  # type: ignore[arg-type]


# ═════════════════════════════════════════════════════════════════════════════
# TENANT PROFILE
# ═════════════════════════════════════════════════════════════════════════════

class TenantProfile(BaseModel):
    tenant_id: str = Field(default="")
    company_name: str = ""
    vat_number: str = ""
    tax_code: str = ""
    address: str = ""
    sender_name: str = ""
    iban: str = ""
    payment_terms: str = ""
    notes: str = ""
    vat_enabled: bool = True
    vat_rate: float = Field(default=0.22, ge=0.0, le=1.0)
    validity_days: int = Field(default=30, gt=0, le=3650)
    logo_data_url: str = ""


def _safe_tenant_dirname(tenant_id: str) -> str:
    cleaned: str = re.sub(r"[^A-Za-z0-9_-]", "", tenant_id)
    if not cleaned:
        raise HTTPException(status_code=400, detail="tenant_id non valido.")
    return cleaned


@profile_router.get("/tenants/{tenant_id}/profile", response_model=TenantProfile)
async def get_tenant_profile(
    tenant_id: str,
    jwt_tenant: str = Depends(get_current_tenant_id),
) -> TenantProfile:
    """Return the tenant profile from Postgres. Returns an empty default if not found."""
    if jwt_tenant != tenant_id:
        raise HTTPException(status_code=403, detail="Accesso negato.")
    safe: str = _safe_tenant_dirname(tenant_id)
    try:
        data: Optional[Dict[str, Any]] = await get_profile(safe)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Errore lettura profilo.") from exc
    if data is None:
        return TenantProfile(tenant_id=safe)
    data["tenant_id"] = safe
    return TenantProfile(**data)


@profile_router.put("/tenants/{tenant_id}/profile", response_model=TenantProfile)
async def put_tenant_profile(
    tenant_id: str,
    body: TenantProfile,
    jwt_tenant: str = Depends(get_current_tenant_id),
) -> TenantProfile:
    """Upsert the tenant profile in Postgres."""
    if jwt_tenant != tenant_id:
        raise HTTPException(status_code=403, detail="Accesso negato.")
    settings = get_settings()
    safe: str = _safe_tenant_dirname(tenant_id)
    body.tenant_id = safe

    if body.logo_data_url and not body.logo_data_url.startswith("data:image/"):
        raise HTTPException(
            status_code=400,
            detail="logo_data_url deve essere un data URL immagine.",
        )

    raw: str = json.dumps(body.model_dump(), ensure_ascii=False)
    if len(raw.encode("utf-8")) > settings.profile_max_bytes:
        raise HTTPException(status_code=413, detail="Profilo troppo grande.")

    try:
        await upsert_profile(safe, body.model_dump())
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Impossibile salvare il profilo."
        ) from exc

    return body


# ═════════════════════════════════════════════════════════════════════════════
# AUTH  (mock /token for dev only — RS256)
# ═════════════════════════════════════════════════════════════════════════════

class TokenRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(default="")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@auth_router.post("/token", response_model=TokenResponse)
@limiter.limit(get_settings().rate_limit_token)
async def get_token(
    request: Request,
    body: TokenRequest,
) -> TokenResponse:
    """
    Emit a JWT RS256 token for the given username (tenant_id).

    Dev/test only. Disable in production by setting TOKEN_ENDPOINT_ENABLED=false.
    Returns 404 when disabled so the endpoint reveals nothing to attackers.

    Rate limited: see ``settings.rate_limit_token`` (default: 5/minute per IP).
    """
    if not get_settings().token_endpoint_enabled:
        raise HTTPException(status_code=404, detail="Not found.")
    token: str = create_access_token(tenant_id=body.username)
    return TokenResponse(access_token=token)


# ═════════════════════════════════════════════════════════════════════════════
# ADMIN — V2: wipe uses pgvector (database.vector_store) instead of ChromaDB
# ═════════════════════════════════════════════════════════════════════════════

class WipeTenantRequest(BaseModel):
    confirm_wipe: bool


class WipeTenantResponse(BaseModel):
    tenant_id: str
    rows_deleted: int
    message: str


@admin_router.delete(
    "/{tenant_id}/vector-data",
    response_model=WipeTenantResponse,
    summary="Elimina tutti i dati vettoriali di un tenant (pgvector hard reset)",
)
async def wipe_tenant_vector_data(
    tenant_id: str,
    body: WipeTenantRequest,
    jwt_tenant: str = Depends(get_current_tenant_id),
) -> WipeTenantResponse:
    # Security: verify the caller owns the tenant they're trying to wipe.
    # Without this check, any authenticated tenant could wipe another tenant's data
    # by supplying an arbitrary tenant_id in the URL path (IDOR vulnerability).
    if jwt_tenant != tenant_id:
        raise HTTPException(status_code=403, detail="Accesso negato.")
    if not body.confirm_wipe:
        raise HTTPException(
            status_code=400,
            detail="confirm_wipe deve essere true per procedere.",
        )

    try:
        rows_deleted: int = await wipe_tenant(tenant_id)
    except Exception as exc:
        log.exception("admin.wipe_failed", tenant_id=tenant_id)
        raise HTTPException(
            status_code=500, detail="Errore durante il wipe."
        ) from exc

    return WipeTenantResponse(
        tenant_id=tenant_id,
        rows_deleted=rows_deleted,
        message=(
            f"Eliminati {rows_deleted} item vettoriali per il tenant '{tenant_id}'."
        ),
    )
