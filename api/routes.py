"""
api/routes.py
-------------
FastAPI routers — four endpoints across two domains:

Lead qualification (prefix /qualify)
--------------------------------------
POST /qualify/stream  — real-time SSE for operators
POST /qualify         — synchronous JSON for CRM webhooks

Catalogue ingestion (prefix /ingest)
--------------------------------------
POST /ingest/stream              — stream ingestion progress via SSE;
                                   emits ``event: interrupt`` if the graph
                                   suspends for human review.
POST /ingest/{thread_id}/approve — resume a suspended ingestion run after
                                   human approval/rejection.

Interrupt / resume flow
-----------------------
When the IngestionGraph reaches ApprovalNode it calls ``interrupt()``, which:
  1. Persists the checkpoint (AsyncSqliteSaver).
  2. Raises ``GraphInterrupt`` out of ``astream``.

The ``/ingest/stream`` generator catches ``GraphInterrupt``, extracts the
review payload, and forwards it as ``event: interrupt`` to the SSE client
(along with the ``thread_id`` needed to resume).

The client later POSTs to ``/ingest/{thread_id}/approve`` with the human
decision.  The handler calls
``graph.ainvoke(Command(resume={...}), config)`` which reloads the
checkpoint, returns the decision to ``interrupt()``, and continues execution.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from pydantic import BaseModel, Field

from core.config import get_settings
from core.graph import AsyncSqliteSaver, build_graph, get_checkpointer
from core.state import LeadInfo, LeadState
from ingestion.graph import build_ingestion_graph, make_initial_state

logger: logging.Logger = logging.getLogger(__name__)

# ── Routers ───────────────────────────────────────────────────────────────────
router: APIRouter = APIRouter(prefix="/qualify", tags=["lead-qualification"])
ingest_router: APIRouter = APIRouter(prefix="/ingest", tags=["catalogue-ingestion"])
upload_router: APIRouter = APIRouter(tags=["catalogue-upload"])


# ── Request / Response schemas ────────────────────────────────────────────────

class QualifyRequest(BaseModel):
    """Inbound lead payload."""

    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Tenant identifier — routes ChromaDB lookup to catalogue_{tenant_id}.",
    )
    raw_text: str = Field(
        ...,
        min_length=10,
        description="Unstructured text from the lead (email body, form submission, etc.).",
    )
    lead_id: Optional[str] = Field(
        default=None,
        description="Optional external lead identifier. Auto-generated if omitted.",
    )


class QualifyResponse(BaseModel):
    """Structured response returned to the CRM after full graph execution."""

    lead_id: str
    total_quote: float
    mapped_services: list[Dict[str, Any]]
    sse_logs: list[str]
    error: Optional[str] = None


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _format_sse(data: str, event: Optional[str] = None) -> str:
    """Encode a single Server-Sent Event frame (spec: https://html.spec.whatwg.org/multipage/server-sent-events.html)."""
    lines: list[str] = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {data}")
    lines.append("")  # blank line = SSE frame boundary
    return "\n".join(lines) + "\n"


def _make_initial_state(lead_info: LeadInfo) -> LeadState:
    """Return a clean initial LeadState for a new graph run.

    ``lead_info`` already carries ``tenant_id``; every node that needs
    the tenant scope reads it from ``state["lead_info"].tenant_id``.
    """
    return {
        "lead_info": lead_info,
        "sanitized_text": "",
        "extracted_services": [],
        "mapped_services": [],
        "total_quote": 0.0,
        "retry_count": 0,
        "sse_logs": [],
        "error": None,
        # Delivery fields (Fase 3) — reset to PENDING at every new run.
        "delivery_status": "PENDING",
        "delivery_attempts": 0,
        "delivery_error": None,
    }


# ── SSE streaming endpoint ────────────────────────────────────────────────────

async def _sse_generator(
    lead_info: LeadInfo,
    thread_id: str,
) -> AsyncGenerator[str, None]:
    """
    Async generator consumed by FastAPI's ``StreamingResponse``.

    Iterates over ``graph.astream(..., stream_mode="values")``, which yields
    a complete state snapshot each time a node finishes.  We diff the
    ``sse_logs`` list between snapshots to extract only the new entries added
    by the most-recently-completed node, then yield one SSE frame per entry.

    This gives the operator real-time feedback as each stage completes,
    without waiting for the entire pipeline to finish.
    """
    checkpointer: AsyncSqliteSaver = await get_checkpointer()
    graph = build_graph(checkpointer=checkpointer)
    initial_state = _make_initial_state(lead_info)
    config: Dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    emitted_log_count: int = 0
    final_snapshot: LeadState = initial_state

    try:
        async for snapshot in graph.astream(initial_state, config=config, stream_mode="values"):
            final_snapshot = snapshot

            # Emit only the new log lines added since the last snapshot.
            all_logs: list[str] = snapshot.get("sse_logs", [])
            new_logs: list[str] = all_logs[emitted_log_count:]
            for log_line in new_logs:
                yield _format_sse(log_line, event="log")
            emitted_log_count += len(new_logs)

    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[routes/stream] Graph error for lead_id=%s: %s", lead_info.id, exc
        )
        yield _format_sse(
            json.dumps({"error": str(exc), "lead_id": lead_info.id}),
            event="error",
        )
        return

    # Final frame: structured payload for the CRM / operator dashboard.
    done_payload: Dict[str, Any] = {
        "lead_id": lead_info.id,
        "total_quote": final_snapshot.get("total_quote", 0.0),
        "mapped_services": final_snapshot.get("mapped_services", []),
        "error": final_snapshot.get("error"),
    }
    yield _format_sse(json.dumps(done_payload), event="done")


@router.post("/stream", summary="Stream lead qualification via SSE")
async def qualify_stream(request_body: QualifyRequest) -> StreamingResponse:
    """
    Qualify a lead and stream processing events to the caller via SSE.

    Event types emitted:

    - ``event: log``   — progress update from a node (one per ``sse_logs`` entry)
    - ``event: done``  — final JSON with ``total_quote`` and ``mapped_services``
    - ``event: error`` — JSON with ``error`` message (on unrecoverable failure)
    """
    lead_id: str = request_body.lead_id or str(uuid.uuid4())
    lead_info = LeadInfo(id=lead_id, raw_text=request_body.raw_text, tenant_id=request_body.tenant_id)
    thread_id: str = f"qualify-{request_body.tenant_id}-{lead_id}"

    logger.info("[routes] SSE stream started for lead_id=%s tenant=%s", lead_id, request_body.tenant_id)

    return StreamingResponse(
        _sse_generator(lead_info, thread_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # prevent Nginx from buffering SSE frames
            "Connection": "keep-alive",
        },
    )


# ── Synchronous CRM endpoint ─────────────────────────────────────────────────

@router.post(
    "",
    response_model=QualifyResponse,
    summary="Qualify a lead (synchronous, for CRM integration)",
)
async def qualify(request_body: QualifyRequest) -> QualifyResponse:
    """
    Run the full qualification graph and return structured JSON.

    Intended for CRM webhooks and automated pipelines that cannot consume SSE.
    Uses ``await graph.ainvoke(...)`` — no threads, no blocking.
    """
    lead_id: str = request_body.lead_id or str(uuid.uuid4())
    lead_info = LeadInfo(id=lead_id, raw_text=request_body.raw_text, tenant_id=request_body.tenant_id)
    thread_id: str = f"qualify-{request_body.tenant_id}-{lead_id}"

    logger.info("[routes] Sync qualification started for lead_id=%s tenant=%s", lead_id, request_body.tenant_id)

    checkpointer: AsyncSqliteSaver = await get_checkpointer()
    graph = build_graph(checkpointer=checkpointer)
    initial_state = _make_initial_state(lead_info)
    config: Dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    try:
        final_state: LeadState = await graph.ainvoke(initial_state, config=config)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[routes] Graph failed for lead_id=%s: %s", lead_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return QualifyResponse(
        lead_id=lead_id,
        total_quote=final_state.get("total_quote", 0.0),
        mapped_services=final_state.get("mapped_services", []),
        sse_logs=final_state.get("sse_logs", []),
        error=final_state.get("error"),
    )


# ═════════════════════════════════════════════════════════════════════════════
# CATALOGUE INGESTION  (ingest_router — prefix: /ingest)
# ═════════════════════════════════════════════════════════════════════════════

# ── Request / Response schemas ────────────────────────────────────────────────

class IngestRequest(BaseModel):
    """Payload to start a new catalogue ingestion run."""

    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Tenant identifier — scopes all DB writes and ChromaDB collections.",
    )
    file_path: str = Field(
        ...,
        description="Absolute path to the catalogue file on the server filesystem.",
    )
    file_format: Literal["csv", "json", "xlsx"] = Field(
        ...,
        description="Explicit format declaration (csv | json | xlsx).",
    )
    review_feedback: Optional[str] = Field(
        default=None,
        description=(
            "Free-text feedback from a previous rejected run.  "
            "Injected into the NormalizerNode prompt when re-triggering."
        ),
    )


class ApprovalDecision(BaseModel):
    """Human reviewer's decision for a suspended ingestion run."""

    approved: bool = Field(
        ...,
        description="True to continue to FinalizeNode; False to reject and end the run.",
    )
    feedback: Optional[str] = Field(
        default=None,
        description=(
            "Optional free-text feedback.  "
            "Stored in state and surfaced to the NormalizerNode on re-trigger."
        ),
    )


class ApprovalResponse(BaseModel):
    """Synchronous response returned after an approval/rejection."""

    thread_id: str
    status: Literal["completed", "rejected"]
    total_items: int
    flagged_count: int
    validation_errors: list[str]
    error: Optional[str] = None


# ── SSE generator ─────────────────────────────────────────────────────────────

async def _ingest_sse_generator(
    tenant_id: str,
    file_path: str,
    file_format: Literal["csv", "json", "xlsx"],
    thread_id: str,
    review_feedback: Optional[str],
) -> AsyncGenerator[str, None]:
    """
    Async SSE generator for a catalogue ingestion run.

    Event types
    -----------
    ``event: log``       — one frame per ``sse_logs`` entry added by a node.
    ``event: interrupt`` — emitted when the graph suspends at ApprovalNode.
                           Payload: ``{thread_id, tenant_id, review_payload}``.
                           The client must POST to ``/ingest/{thread_id}/approve``
                           to resume.
    ``event: done``      — emitted on clean completion (no interrupt).
                           Payload: summary counts and error field.
    ``event: error``     — emitted on unrecoverable exception.

    GraphInterrupt handling
    -----------------------
    ``GraphInterrupt.args[0]`` is a tuple of ``langgraph.types.Interrupt``
    objects (one per ``interrupt()`` call encountered).  We surface the first
    interrupt's ``.value`` — the review payload built in ``approval_node``.
    """
    checkpointer: AsyncSqliteSaver = await get_checkpointer()
    graph = build_ingestion_graph(checkpointer)
    initial_state = make_initial_state(tenant_id, file_path, file_format, review_feedback)
    config: Dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    emitted_log_count: int = 0
    final_snapshot = initial_state

    try:
        async for snapshot in graph.astream(
            initial_state, config=config, stream_mode="values"
        ):
            final_snapshot = snapshot
            all_logs: list[str] = snapshot.get("sse_logs", [])
            new_logs: list[str] = all_logs[emitted_log_count:]
            for log_line in new_logs:
                yield _format_sse(log_line, event="log")
            emitted_log_count += len(new_logs)

    except GraphInterrupt as gi:
        # Graph suspended at ApprovalNode — forward the interrupt payload.
        # gi.args[0] is a tuple of langgraph.types.Interrupt; each has .value.
        interrupts = gi.args[0] if gi.args else ()
        review_payload: Any = interrupts[0].value if interrupts else {}

        logger.warning(
            "[routes/ingest] Graph interrupted for tenant=%s thread=%s",
            tenant_id, thread_id,
        )
        yield _format_sse(
            json.dumps({
                "thread_id": thread_id,
                "tenant_id": tenant_id,
                "review_payload": review_payload,
            }),
            event="interrupt",
        )
        return  # connection stays open until yield; client handles resume

    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[routes/ingest] Unhandled error tenant=%s thread=%s: %s",
            tenant_id, thread_id, exc,
        )
        yield _format_sse(
            json.dumps({"error": str(exc), "tenant_id": tenant_id, "thread_id": thread_id}),
            event="error",
        )
        return

    # ── LangGraph 1.x: interrupt detection ────────────────────────────────────
    # From LangGraph 1.x, ``interrupt()`` no longer raises ``GraphInterrupt`` out
    # of ``astream`` — the suspension is surfaced via the graph state instead.
    # So after the stream ends without an exception, inspect the checkpoint: if
    # there are pending interrupts, the graph suspended at ApprovalNode → forward
    # the review payload as ``event: interrupt`` (and do NOT emit ``done``).
    try:
        snapshot_state = await graph.aget_state(config)
        pending = list(getattr(snapshot_state, "interrupts", ()) or ())
        if not pending:  # fallback: some versions expose them at task level
            for task in getattr(snapshot_state, "tasks", ()) or ():
                pending.extend(getattr(task, "interrupts", ()) or ())
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[routes/ingest] aget_state failed tenant=%s thread=%s: %s",
            tenant_id, thread_id, exc,
        )
        pending = []

    if pending:
        first = pending[0]
        review_payload = getattr(first, "value", first)
        logger.warning(
            "[routes/ingest] Graph suspended (interrupt) tenant=%s thread=%s | flagged review",
            tenant_id, thread_id,
        )
        yield _format_sse(
            json.dumps({
                "thread_id": thread_id,
                "tenant_id": tenant_id,
                "review_payload": review_payload,
            }),
            event="interrupt",
        )
        return

    # Clean completion (no interrupt needed)
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


# ── POST /ingest/stream ───────────────────────────────────────────────────────

@ingest_router.post("/stream", summary="Ingest a service catalogue via SSE")
async def ingest_stream(request_body: IngestRequest) -> StreamingResponse:
    """
    Start a catalogue ingestion run and stream progress events via SSE.

    The ``thread_id`` generated for this run is exposed in the
    ``X-Thread-Id`` response header so the client can save it before
    reading the SSE stream — needed to call ``/approve`` later.

    If the graph encounters low-confidence items or flags, it suspends and
    emits ``event: interrupt`` with a ``review_payload`` and the ``thread_id``.
    The client should display the flagged items to the operator, collect the
    decision, and POST it to ``/ingest/{thread_id}/approve``.
    """
    thread_id: str = f"ingest-{request_body.tenant_id}-{uuid.uuid4()}"
    logger.info(
        "[routes] Ingestion SSE started | tenant=%s | thread=%s | file=%s",
        request_body.tenant_id, thread_id, request_body.file_path,
    )

    return StreamingResponse(
        _ingest_sse_generator(
            tenant_id=request_body.tenant_id,
            file_path=request_body.file_path,
            file_format=request_body.file_format,
            thread_id=thread_id,
            review_feedback=request_body.review_feedback,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Thread-Id": thread_id,  # client reads this before consuming the stream
        },
    )


# ── POST /ingest/{thread_id}/approve ─────────────────────────────────────────

@ingest_router.post(
    "/{thread_id}/approve",
    response_model=ApprovalResponse,
    summary="Resume a suspended ingestion run after human review",
)
async def approve_ingestion(
    thread_id: str,
    body: ApprovalDecision,
) -> ApprovalResponse:
    """
    Resume an ingestion graph that was suspended at ``ApprovalNode``.

    Internally calls::

        await graph.ainvoke(
            Command(resume={"approved": body.approved, "feedback": body.feedback}),
            config={"configurable": {"thread_id": thread_id}},
        )

    LangGraph reloads the checkpoint identified by ``thread_id``, injects the
    ``resume`` value as the return of ``interrupt()``, and continues execution:

    - If ``approved=True``  → graph runs ``FinalizeNode`` → writes to ChromaDB.
    - If ``approved=False`` → graph routes to ``END`` immediately.
      The caller should re-trigger ``/ingest/stream`` (optionally with
      ``review_feedback``) to restart normalisation with corrected parameters.

    Raises 404 if no checkpoint exists for the given ``thread_id``.
    Raises 500 on unrecoverable graph errors.
    """
    logger.info(
        "[routes] Ingestion approval | thread=%s | approved=%s",
        thread_id, body.approved,
    )

    checkpointer: AsyncSqliteSaver = await get_checkpointer()
    graph = build_ingestion_graph(checkpointer)
    config: Dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    try:
        final_state = await graph.ainvoke(
            Command(resume={"approved": body.approved, "feedback": body.feedback or ""}),
            config=config,
        )
    except ValueError as exc:
        # LangGraph raises ValueError when no checkpoint is found for thread_id
        logger.warning("[routes] No checkpoint for thread=%s: %s", thread_id, exc)
        raise HTTPException(
            status_code=404,
            detail=f"No suspended ingestion found for thread_id='{thread_id}'.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("[routes] Approval failed for thread=%s: %s", thread_id, exc)
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
# CATALOGUE UPLOAD  (upload_router — path: /upload)
# ═════════════════════════════════════════════════════════════════════════════
#
# Bridges the frontend dropzone to the ingestion pipeline.  The UI uploads a
# raw catalogue file here; the server stores it in a tenant-scoped directory and
# returns the absolute ``file_path`` that the client then passes to
# ``POST /ingest/stream`` (whose ``IngestRequest`` expects a server-side path).
#
# Security contract
# -----------------
# - The client-supplied filename is NEVER trusted: only its extension is read,
#   and the stored file is renamed to a server-generated UUID (no path traversal).
# - tenant_id is sanitised to ``[A-Za-z0-9_-]`` before being used as a directory.
# - Files are validated by extension and size; the content is not opened here
#   (parsing happens later in the ChunkingNode).

_ALLOWED_EXTENSIONS: Dict[str, str] = {".csv": "csv", ".json": "json", ".xlsx": "xlsx"}


class UploadResponse(BaseModel):
    """Returned after a successful catalogue upload."""

    file_path: str = Field(..., description="Absolute server path to pass to /ingest/stream.")
    file_format: Literal["csv", "json", "xlsx"] = Field(..., description="Format inferred from the extension.")


def _safe_tenant_dirname(tenant_id: str) -> str:
    """Sanitise a tenant_id for safe use as a directory name (no traversal)."""
    cleaned: str = re.sub(r"[^A-Za-z0-9_-]", "", tenant_id)
    if not cleaned:
        raise HTTPException(status_code=400, detail="tenant_id non valido.")
    return cleaned


@upload_router.post(
    "/upload",
    response_model=UploadResponse,
    tags=["catalogue-upload"],
    summary="Upload a catalogue file (returns a server path for /ingest/stream)",
)
async def upload_catalogue(
    file: UploadFile = File(...),
    tenant_id: str = Form(...),
) -> UploadResponse:
    """
    Accept a multipart catalogue file, store it tenant-scoped, return its path.

    Validates extension (csv | json | xlsx) and size (``upload_max_bytes``),
    sanitises both tenant_id and filename, and writes the file under
    ``{upload_dir}/{tenant_id}/{uuid}{ext}``.
    """
    settings = get_settings()

    # 1. Extension allow-list (read only the suffix of the client filename).
    ext: str = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Estensione non supportata: '{ext or '∅'}'. Usa csv | json | xlsx.",
        )
    file_format: str = _ALLOWED_EXTENSIONS[ext]

    # 2. Read content and enforce size limits.
    contents: bytes = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="File vuoto.")
    if len(contents) > settings.upload_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File troppo grande (max {settings.upload_max_bytes} byte).",
        )

    # 3. Tenant-scoped destination with a server-generated, traversal-safe name.
    safe_tenant: str = _safe_tenant_dirname(tenant_id)
    dest_dir: Path = (settings.upload_dir / safe_tenant).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path: Path = dest_dir / f"{uuid.uuid4().hex}{ext}"

    # 4. Persist.
    try:
        dest_path.write_bytes(contents)
    except OSError as exc:
        logger.exception("[upload] write failed | tenant=%s: %s", safe_tenant, exc)
        raise HTTPException(status_code=500, detail="Impossibile salvare il file.") from exc

    logger.info(
        "[upload] tenant=%s | saved=%s | format=%s | bytes=%d",
        safe_tenant, dest_path, file_format, len(contents),
    )
    return UploadResponse(file_path=str(dest_path), file_format=file_format)  # type: ignore[arg-type]


# ═════════════════════════════════════════════════════════════════════════════
# TENANT PROFILE  (profile_router — path: /tenants/{tenant_id}/profile)
# ═════════════════════════════════════════════════════════════════════════════
#
# Profilo aziendale per-tenant usato per generare preventivi brandizzati
# (intestazione PDF, logo, dati fiscali, IBAN/condizioni). Storage: un file JSON
# per tenant in ``profiles_dir`` — semplice, air-gapped, senza DB esterni.

profile_router: APIRouter = APIRouter(tags=["tenant-profile"])


class TenantProfile(BaseModel):
    """Profilo aziendale di un tenant (mittente del preventivo)."""

    tenant_id: str = Field(default="", description="Impostato dal server dal path.")
    company_name: str = ""
    vat_number: str = ""          # P.IVA
    tax_code: str = ""            # Codice Fiscale
    address: str = ""            # sede legale (multilinea)
    sender_name: str = ""        # mittente / firma
    iban: str = ""
    payment_terms: str = ""      # condizioni di pagamento
    notes: str = ""              # note / termini standard
    vat_enabled: bool = True
    vat_rate: float = Field(default=0.22, ge=0.0, le=1.0)
    validity_days: int = Field(default=30, gt=0, le=3650)
    logo_data_url: str = ""      # data:image/...;base64,...


def _profile_path(safe_tenant: str) -> Path:
    """Path assoluto del file JSON del profilo per un tenant già sanificato."""
    return (get_settings().profiles_dir / f"{safe_tenant}.json").resolve()


@profile_router.get(
    "/tenants/{tenant_id}/profile",
    response_model=TenantProfile,
    tags=["tenant-profile"],
    summary="Get a tenant's company profile (returns empty defaults if unset)",
)
async def get_tenant_profile(tenant_id: str) -> TenantProfile:
    """Restituisce il profilo del tenant; se non esiste ritorna i default vuoti."""
    safe: str = _safe_tenant_dirname(tenant_id)
    path: Path = _profile_path(safe)
    if path.exists():
        try:
            data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("[profile] read failed tenant=%s: %s", safe, exc)
            raise HTTPException(status_code=500, detail="Profilo illeggibile.") from exc
        data["tenant_id"] = safe
        return TenantProfile(**data)
    return TenantProfile(tenant_id=safe)


@profile_router.put(
    "/tenants/{tenant_id}/profile",
    response_model=TenantProfile,
    tags=["tenant-profile"],
    summary="Create or update a tenant's company profile",
)
async def put_tenant_profile(tenant_id: str, body: TenantProfile) -> TenantProfile:
    """Salva (upsert) il profilo del tenant come file JSON tenant-scoped."""
    settings = get_settings()
    safe: str = _safe_tenant_dirname(tenant_id)
    body.tenant_id = safe

    if body.logo_data_url and not body.logo_data_url.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="logo_data_url deve essere un data URL immagine.")

    raw: str = json.dumps(body.model_dump(), ensure_ascii=False)
    if len(raw.encode("utf-8")) > settings.profile_max_bytes:
        raise HTTPException(status_code=413, detail="Profilo troppo grande (logo eccessivo).")

    try:
        settings.profiles_dir.mkdir(parents=True, exist_ok=True)
        _profile_path(safe).write_text(raw, encoding="utf-8")
    except OSError as exc:
        logger.exception("[profile] write failed tenant=%s: %s", safe, exc)
        raise HTTPException(status_code=500, detail="Impossibile salvare il profilo.") from exc

    logger.info("[profile] saved tenant=%s | bytes=%d", safe, len(raw.encode("utf-8")))
    return body
