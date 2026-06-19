"""
worker/tasks.py
---------------
ARQ task functions — V2.1.

These are the background jobs executed by the ARQ worker process.
FastAPI enqueues them and responds immediately with 202 Accepted.
The worker updates graph state in Postgres at every node via
AsyncPostgresSaver; the /status endpoint polls it.

Tasks
-----
run_qualification_task        — execute the full qualification graph
run_qualification_task_resume — resume a HITL-interrupted graph after approval
run_ingestion_task            — run the catalogue ingestion graph

Graph singleton
---------------
The compiled qualification graph is built once per worker process and stored
in _qualification_graph. ARQ workers are long-lived processes, so rebuilding
the StateGraph on every job is wasteful — compilation traverses the graph
topology and wires up all conditional edges. The checkpointer is shared via
get_checkpointer() (already a singleton). The ingestion graph follows the same
pattern via _ingestion_graph.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import structlog

from core.state import AgentState, LeadContext
from core.graph import build_graph, get_checkpointer
from services.embeddings import EmbeddingError, aembed_documents

try:
    from database.db_core import get_pool
except Exception:
    get_pool = None  # type: ignore[assignment]

log = structlog.get_logger()

# ── Graph singletons ──────────────────────────────────────────────────────────
# Initialised lazily on the first task invocation. The worker process is
# long-lived, so these are compiled exactly once per process lifetime.

_qualification_graph = None
_ingestion_graph = None


async def _get_qualification_graph():
    """Return the worker-process-scoped compiled qualification graph."""
    global _qualification_graph
    if _qualification_graph is None:
        checkpointer = await get_checkpointer()
        _qualification_graph = build_graph(checkpointer=checkpointer)
        log.info("worker.qualification_graph_compiled")
    return _qualification_graph


async def _get_ingestion_graph():
    """Return the worker-process-scoped compiled ingestion graph."""
    global _ingestion_graph
    if _ingestion_graph is None:
        from ingestion.graph import build_ingestion_graph
        checkpointer = await get_checkpointer()
        _ingestion_graph = build_ingestion_graph(checkpointer)
        log.info("worker.ingestion_graph_compiled")
    return _ingestion_graph


# ── Crash recovery helper ─────────────────────────────────────────────────────

async def _update_state_on_crash(
    graph: Any,
    config: dict[str, Any],
    exc: Exception,
) -> None:
    """
    Persist ``status="error"`` to the LangGraph checkpoint after a fatal crash.

    Called from the except block of qualification tasks so the polling endpoint
    (/status/{thread_id}) stops waiting and surfaces the error to the frontend
    instead of looping forever on "processing" or "queued".

    PII safety: only the exception *type* is written to the checkpoint, never
    str(exc) which may contain the lead payload or other sensitive data.

    This function is intentionally fire-and-forget from the caller's perspective:
    if the checkpoint update itself fails (e.g. Postgres is down), we log and
    swallow the secondary exception so the original exception can propagate
    cleanly via re-raise.
    """
    try:
        await graph.aupdate_state(
            config,
            {
                "status": "error",
                "error_detail": type(exc).__name__,
            },
        )
        log.info(
            "qualification.state_marked_error",
            thread_id=config.get("configurable", {}).get("thread_id"),
        )
    except Exception as update_exc:
        # Secondary failure: log but do NOT re-raise so the original exception
        # propagates unchanged to the ARQ caller.
        log.error(
            "qualification.state_update_failed",
            error_type=type(update_exc).__name__,
            thread_id=config.get("configurable", {}).get("thread_id"),
        )


# ── Tasks ─────────────────────────────────────────────────────────────────────

async def run_qualification_task(
    ctx: dict[str, Any],
    thread_id: str,
    lead_context_dict: dict[str, Any],
    tenant_id: str,
) -> dict[str, Any]:
    """
    ARQ task: execute the qualification graph in the background.

    FastAPI enqueues this and responds immediately with 202.
    The worker updates state in Postgres at each node.
    The /status endpoint reads the checkpoint for polling.
    """
    lead = LeadContext(**lead_context_dict)
    log.info("qualification.start", thread_id=thread_id, tenant_id=tenant_id)

    graph = await _get_qualification_graph()
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    initial_state: AgentState = {
        "lead": lead,
        "messages": [],
        "retrieved_docs": [],
        "confidence_score": 0.0,
        "human_approved": None,
        "review_feedback": None,
        "status": "processing",
        "error_detail": None,
        "sanitized_text": "",
        "extracted_services": [],
        "mapped_services": [],
        "total_quote": 0.0,
        "on_request_services": [],
        "retry_count": 0,
        "delivery_status": "PENDING",
        "delivery_attempts": 0,
        "delivery_error": None,
    }

    try:
        await graph.ainvoke(initial_state, config=config)
        log.info("qualification.done", thread_id=thread_id, tenant_id=tenant_id)
        return {"status": "completed", "thread_id": thread_id}
    except Exception as exc:
        # PII-safe: log only the exception type, never str(exc) which may
        # contain the raw lead payload or other sensitive data.
        log.error(
            "qualification.fatal_error",
            thread_id=thread_id,
            tenant_id=tenant_id,
            error_type=type(exc).__name__,
        )
        # Write status="error" to the Postgres checkpoint so the polling
        # endpoint surfaces the failure instead of looping on "processing".
        await _update_state_on_crash(graph, config, exc)
        raise


async def run_qualification_task_resume(
    ctx: dict[str, Any],
    thread_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    """
    ARQ task: resume a graph suspended in pending_review after human approval.

    The checkpoint in Postgres already contains human_approved=True and
    status='queued' (written by POST /lead/{thread_id}/approve via
    graph.aupdate_state). Passing a non-None dict in Command(resume=...) would
    be redundant and could shadow the state already committed to Postgres.

    Command(resume=None) tells LangGraph to resume from the checkpoint as-is,
    without injecting any additional value into the interrupted node.

    V2.1 change: was Command(resume={"human_approved": True}) — removed the
    redundant dict.
    """
    from langgraph.types import Command

    log.info("qualification.resume", thread_id=thread_id, tenant_id=tenant_id)

    graph = await _get_qualification_graph()
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}

    try:
        await graph.ainvoke(Command(resume=None), config=config)
        log.info(
            "qualification.resume_done", thread_id=thread_id, tenant_id=tenant_id
        )
        return {"status": "completed", "thread_id": thread_id}
    except Exception as exc:
        log.error(
            "qualification.resume_fatal_error",
            thread_id=thread_id,
            tenant_id=tenant_id,
            error_type=type(exc).__name__,
        )
        await _update_state_on_crash(graph, config, exc)
        raise


async def update_embedding_task(
    ctx: dict[str, Any],
    item_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    """
    ARQ task: regenerate the pgvector embedding for a catalogue item after a PATCH.

    Flow
    ----
    1. Fetch the updated record from ``catalogue_items``.
    2. Rebuild the embedding text via ``_row_to_text`` (schema-agnostic key:value
       format, identical to what the ingestion pipeline uses at index time).
    3. Compute a new embedding via Ollama (``aembed_documents``).
    4. Write the new vector back to ``catalogue_items.embedding``.

    Eventual consistency
    --------------------
    The API updates the Postgres record synchronously, then enqueues this task.
    Between enqueue and execution the vector may be stale — this is acceptable
    because catalogue updates are infrequent and search is eventually consistent.

    Error handling
    --------------
    If Ollama is unreachable, ``EmbeddingError`` propagates and ARQ will retry
    the job according to WorkerSettings (default: no retry, error surfaced in Redis).
    """
    from ingestion.graph import _row_to_text

    # ARQ passes the current attempt number (1-based) in ctx["job_try"].
    # We surface it in logs so retries are visible in Loki/Grafana without
    # having to correlate by job ID.
    job_try: int = ctx.get("job_try", 1)
    log.info(
        "embedding_update.start",
        item_id=item_id,
        tenant_id=tenant_id,
        attempt=job_try,
    )

    _pool_fn = get_pool
    if _pool_fn is None:
        from database.db_core import get_pool as _pool_fn  # type: ignore[assignment]
    pool = await _pool_fn()

    # ── 1. Fetch the current (post-PATCH) record ──────────────────────────────
    row = await pool.fetchrow(
        """
        SELECT service, price, description, metadata
        FROM   catalogue_items
        WHERE  id = $1::uuid AND tenant_id = $2
        """,
        item_id,
        tenant_id,
    )
    if row is None:
        log.warning(
            "embedding_update.item_not_found",
            item_id=item_id,
            tenant_id=tenant_id,
        )
        return {"status": "not_found", "item_id": item_id}

    # ── 2. Rebuild embedding text (mirrors ingestion finalizer_node) ──────────
    # Expand metadata fields into the top-level dict so all raw columns
    # (including tenant-specific custom fields) are included in the vector.
    raw_row: dict = {
        "service": row["service"],
        "price": str(row["price"]),
        "description": row["description"] or "",
        **json.loads(row["metadata"] or "{}"),
    }
    text: str = _row_to_text(raw_row)

    # ── 3. Compute new embedding ──────────────────────────────────────────────
    try:
        vectors = await aembed_documents([text], tenant_id=tenant_id)
    except EmbeddingError as exc:
        log.error(
            "embedding_update.embed_failed",
            item_id=item_id,
            tenant_id=tenant_id,
            error_type=type(exc).__name__,
            attempt=job_try,
            max_tries=update_embedding_task.max_tries,  # type: ignore[attr-defined]
        )
        raise  # ARQ will retry up to max_tries before marking the job failed

    embedding: list[float] = vectors[0]

    # ── 4. Persist the new vector (only embedding column is touched) ──────────
    await pool.execute(
        """
        UPDATE catalogue_items
           SET embedding = $1::vector
         WHERE id = $2::uuid AND tenant_id = $3
        """,
        json.dumps(embedding),
        item_id,
        tenant_id,
    )

    log.info(
        "embedding_update.done",
        item_id=item_id,
        tenant_id=tenant_id,
        dim=len(embedding),
        attempt=job_try,
    )
    return {"status": "updated", "item_id": item_id}


# ARQ reads this attribute to determine the per-function retry limit.
# Raising EmbeddingError (or any exception) inside update_embedding_task will
# cause ARQ to re-enqueue the job up to max_tries times before marking it
# permanently failed. This is intentionally narrower than the global
# WorkerSettings.max_tries (which defaults to 5) because embedding updates are
# idempotent and Ollama transient failures typically resolve within 2 retries.
update_embedding_task.max_tries = 3  # type: ignore[attr-defined]


async def run_ingestion_task(
    ctx: dict[str, Any],
    thread_id: str,
    tenant_id: str,
    object_key: str,
    file_format: Literal["csv", "json", "xlsx"],
    review_feedback: str | None = None,
) -> dict[str, Any]:
    """
    ARQ task: run the catalogue ingestion graph in the background.

    The ingestion graph uses the same AsyncPostgresSaver checkpointer.
    If it reaches ApprovalNode, it suspends and sets status='pending_review'.
    The operator uses /ingest/{thread_id}/approve to resume.

    V2.1: ``object_key`` (S3 Object Key) replaces the old ``file_path``
    (absolute filesystem path). ``make_initial_state`` is responsible for
    downloading the file from S3 before processing.
    """
    from ingestion.graph import make_initial_state

    log.info(
        "ingestion.start",
        thread_id=thread_id,
        tenant_id=tenant_id,
        object_key=object_key,
    )

    graph = await _get_ingestion_graph()
    initial_state = make_initial_state(tenant_id, object_key, file_format, review_feedback)
    config = {"configurable": {"thread_id": thread_id}}

    try:
        await graph.ainvoke(initial_state, config=config)
        log.info("ingestion.done", thread_id=thread_id)
        return {"status": "completed", "thread_id": thread_id}
    except Exception as exc:
        log.exception(
            "ingestion.error", thread_id=thread_id, error=str(exc)
        )
        raise
