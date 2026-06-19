"""
worker/tasks.py
---------------
ARQ task functions — V2.

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

from typing import Literal

import structlog

from core.state import AgentState, LeadContext
from core.graph import build_graph, get_checkpointer

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


# ── Tasks ─────────────────────────────────────────────────────────────────────

async def run_qualification_task(
    ctx: dict,
    thread_id: str,
    lead_context_dict: dict,
    tenant_id: str,
) -> dict:
    """
    ARQ task: execute the qualification graph in the background.

    FastAPI enqueues this and responds immediately with 202.
    The worker updates state in Postgres at each node.
    The /status endpoint reads the checkpoint for polling.
    """
    lead = LeadContext(**lead_context_dict)
    log.info("qualification.start", thread_id=thread_id, tenant_id=tenant_id)

    graph = await _get_qualification_graph()
    config = {"configurable": {"thread_id": thread_id}}

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
        log.exception(
            "qualification.error",
            thread_id=thread_id,
            tenant_id=tenant_id,
            error=str(exc),
        )
        raise


async def run_qualification_task_resume(
    ctx: dict,
    thread_id: str,
    tenant_id: str,
) -> dict:
    """
    ARQ task: resume a graph suspended in pending_review after human approval.

    The checkpoint in Postgres already contains human_approved=True (written
    by /approve). The graph resumes from the node after the interrupt.
    """
    from langgraph.types import Command

    log.info("qualification.resume", thread_id=thread_id, tenant_id=tenant_id)

    graph = await _get_qualification_graph()
    config = {"configurable": {"thread_id": thread_id}}

    try:
        await graph.ainvoke(Command(resume={"human_approved": True}), config=config)
        log.info(
            "qualification.resume_done", thread_id=thread_id, tenant_id=tenant_id
        )
        return {"status": "completed", "thread_id": thread_id}
    except Exception as exc:
        log.exception(
            "qualification.resume_error",
            thread_id=thread_id,
            tenant_id=tenant_id,
            error=str(exc),
        )
        raise


async def run_ingestion_task(
    ctx: dict,
    thread_id: str,
    tenant_id: str,
    file_path: str,
    file_format: Literal["csv", "json", "xlsx"],
    review_feedback: str | None = None,
) -> dict:
    """
    ARQ task: run the catalogue ingestion graph in the background.

    The ingestion graph uses the same AsyncPostgresSaver checkpointer.
    If it reaches ApprovalNode, it suspends and sets status='pending_review'.
    The operator uses /ingest/{thread_id}/approve to resume.
    """
    from ingestion.graph import make_initial_state

    log.info(
        "ingestion.start",
        thread_id=thread_id,
        tenant_id=tenant_id,
        file=file_path,
    )

    graph = await _get_ingestion_graph()
    initial_state = make_initial_state(tenant_id, file_path, file_format, review_feedback)
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
