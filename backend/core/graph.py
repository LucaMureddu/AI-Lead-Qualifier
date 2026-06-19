"""
core/graph.py
-------------
LangGraph orchestration layer — V2.

Topology
--------
  SanitizerNode
       │
  ExtractorNode  ◄──────────────────────────────────────────┐
       │                                                     │ retry (retry_count < max)
  MapperNode                                                 │
       │                                                     │
  EvaluatorNode ── score >= 0.75 ──► CalculatorNode         │
       │                              │                      │
       │                         DeliveryNode ◄─────────────┼── retry (attempts < max)
       │                              │                      │
       │                         SUCCESS → END              │
       │                                                    │
       ├── score < 0.75 & retry_count < max ───────────────┘
       │
       └── score < 0.75 & retry_count == max ──► hitl_interrupt (pending_review)

V2 changes vs V1
----------------
- AsyncSqliteSaver → AsyncPostgresSaver (Postgres checkpointing)
- Added EvaluatorNode + route_after_evaluator (confidence-based HITL)
- State type: LeadState → AgentState
- human_fallback_node now sets status='pending_review' (not just interrupt)

Why setup() is kept (not removed in favour of Alembic)
-------------------------------------------------------
Alembic manages the *application* schema (catalogue_items — see 001_initial_schema.py).
AsyncPostgresSaver.setup() manages the *LangGraph* schema (checkpoints,
checkpoint_blobs, checkpoint_writes, checkpoint_migrations).

Encoding those tables in an Alembic migration would couple our migration
files to LangGraph's internal schema version — a maintenance trap.  We let
the library own its schema and call setup() once at startup instead.

setup() is idempotent for the table DDL (uses IF NOT EXISTS) but inserts a
row into checkpoint_migrations on every call.  When multiple instances start
simultaneously (rolling deploy), only the first INSERT succeeds; the others
hit UniqueViolation.  We catch that specific exception via the psycopg3
type rather than fragile string matching.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Literal

import structlog
from psycopg.errors import UniqueViolation
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from agents.calculator import calculator_node
from agents.delivery import delivery_node
from agents.evaluator import evaluator_node
from agents.extractor import extractor_node
from agents.mapper import mapper_node
from agents.sanitizer import sanitizer_node
from core.config import get_settings
from core.state import AgentState

log = structlog.get_logger()


# ── Routing helpers ───────────────────────────────────────────────────────────


def route_after_evaluator(
    state: AgentState,
) -> Literal["calculator", "extractor", "hitl_interrupt"]:
    """
    Conditional edge executed after EvaluatorNode.

    Decision matrix:
    ┌──────────────────────────────────────┬────────────────────────────────┐
    │ confidence_score >= 0.75             │ → CalculatorNode               │
    │ score < 0.75 & retry_count < max    │ → ExtractorNode (retry)        │
    │ score < 0.75 & retry_count == max   │ → hitl_interrupt (HITL)        │
    └──────────────────────────────────────┴────────────────────────────────┘
    """
    settings = get_settings()
    score: float = state.get("confidence_score", 0.0)
    retry: int = state.get("retry_count", 0)

    if score >= 0.75:
        log.debug("route.evaluator", next="calculator", score=round(score, 3))
        return "calculator"

    if retry < settings.max_retry_count:
        log.debug("route.evaluator", next="extractor", retry=retry, score=round(score, 3))
        return "extractor"

    log.warning("route.evaluator", next="hitl_interrupt", score=round(score, 3), retries=retry)
    return "hitl_interrupt"


def route_after_delivery(state: AgentState) -> Literal["delivery", "__end__"]:
    """
    Conditional edge executed after DeliveryNode.

    Decision matrix:
    ┌─────────────────────────────────────────┬──────────────────────────────┐
    │ delivery_status == "SUCCESS"            │ → END                        │
    │ FAILED & delivery_attempts < max        │ → DeliveryNode (retry)       │
    │ FAILED & delivery_attempts >= max       │ → END (log abandonment)      │
    └─────────────────────────────────────────┴──────────────────────────────┘
    """
    settings = get_settings()
    status: str = state.get("delivery_status", "PENDING")
    attempts: int = state.get("delivery_attempts", 0)

    if status == "SUCCESS":
        log.debug("route.delivery", next="end", status="success")
        return "__end__"

    if attempts < settings.delivery_max_attempts:
        log.warning(
            "route.delivery",
            next="delivery",
            attempt=attempts,
            max_attempts=settings.delivery_max_attempts,
        )
        return "delivery"

    log.error(
        "route.delivery",
        next="end",
        reason="abandoned",
        attempts=attempts,
        max_attempts=settings.delivery_max_attempts,
    )
    return "__end__"


# ── HITL interrupt node ───────────────────────────────────────────────────────


def hitl_interrupt_node(state: AgentState) -> dict:
    """
    Suspends graph execution via LangGraph interrupt().

    Sets status='pending_review' so the /status polling endpoint can surface
    the review payload to the operator. The operator calls /lead/{id}/approve
    to inject human_approved + resume the graph.

    interrupt() never returns — it suspends the graph at this node and persists
    the checkpoint in Postgres for later resumption.
    """
    lead_id: str = state["lead"].lead_id
    tenant_id: str = state["lead"].tenant_id
    log.warning(
        "hitl_interrupt.activated",
        lead_id=lead_id,
        tenant_id=tenant_id,
        confidence_score=state.get("confidence_score", 0.0),
    )

    review_payload = {
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "confidence_score": state.get("confidence_score", 0.0),
        "extracted_services": state.get("extracted_services", []),
        "mapped_services": state.get("mapped_services", []),
    }

    # interrupt() raises internally — suspends the graph and persists the checkpoint.
    interrupt(value=review_payload)

    # Unreachable — satisfies the type checker.
    return {"status": "pending_review"}  # type: ignore[return-value]


# ── Checkpointer factory ──────────────────────────────────────────────────────

_checkpointer: AsyncPostgresSaver | None = None
_checkpointer_exit_stack: AsyncExitStack | None = None


async def get_checkpointer() -> AsyncPostgresSaver:
    """
    Return the shared AsyncPostgresSaver, initialising it on first call.

    AsyncPostgresSaver requires psycopg3 (NOT asyncpg). from_conn_string()
    manages its own psycopg3 connection pool internally. We enter the context
    manager manually (via AsyncExitStack) so the pool lives for the entire
    application lifetime. Call close_checkpointer() on shutdown.

    setup() creates the LangGraph checkpoint tables. It is idempotent for DDL
    (IF NOT EXISTS) but inserts into checkpoint_migrations on each call.
    Concurrent instances (rolling deploy) will see UniqueViolation on that
    INSERT — caught explicitly via psycopg.errors.UniqueViolation, not by
    fragile string matching.
    """
    global _checkpointer, _checkpointer_exit_stack

    if _checkpointer is None:
        settings = get_settings()

        # AsyncExitStack lets us manually enter the context manager once and
        # later exit it cleanly in close_checkpointer().
        stack = AsyncExitStack()
        _checkpointer = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(settings.database_dsn)
        )
        _checkpointer_exit_stack = stack

        try:
            await _checkpointer.setup()
            log.info("checkpointer.setup_complete")
        except UniqueViolation:
            # Another instance already ran setup() and inserted the
            # checkpoint_migrations row. Tables exist — safe to continue.
            log.info("checkpointer.setup_skipped", reason="already_initialised_by_peer")
        except Exception:
            # Any other error during setup is unexpected — re-raise.
            await stack.aclose()
            _checkpointer = None
            _checkpointer_exit_stack = None
            raise

    return _checkpointer


async def close_checkpointer() -> None:
    """Release the psycopg3 pool held by the checkpointer (call on shutdown)."""
    global _checkpointer, _checkpointer_exit_stack

    if _checkpointer_exit_stack is not None:
        await _checkpointer_exit_stack.aclose()
        _checkpointer = None
        _checkpointer_exit_stack = None
        log.info("checkpointer.closed")


# ── Graph factory ─────────────────────────────────────────────────────────────


def build_graph(checkpointer: AsyncPostgresSaver | None = None):
    """
    Construct and compile the LangGraph StateGraph — V2.

    Called once from main.py lifespan; the compiled graph is stored on
    app.state and reused across all requests.  The graph itself is stateless —
    all per-thread state lives in the Postgres checkpoint.

    Parameters
    ----------
    checkpointer : AsyncPostgresSaver | None
        Postgres checkpointer for persistence. Pass None in unit tests to get
        an in-memory-only graph.

    Returns
    -------
    CompiledGraph
        Ready-to-invoke compiled graph.
    """
    builder: StateGraph = StateGraph(AgentState)

    # ── Register nodes ────────────────────────────────────────────────────────
    builder.add_node("sanitizer", sanitizer_node)
    builder.add_node("extractor", extractor_node)
    builder.add_node("mapper", mapper_node)
    builder.add_node("evaluator", evaluator_node)
    builder.add_node("calculator", calculator_node)
    builder.add_node("delivery", delivery_node)
    builder.add_node("hitl_interrupt", hitl_interrupt_node)

    # ── Static edges ──────────────────────────────────────────────────────────
    builder.set_entry_point("sanitizer")
    builder.add_edge("sanitizer", "extractor")
    builder.add_edge("extractor", "mapper")
    builder.add_edge("mapper", "evaluator")
    builder.add_edge("calculator", "delivery")
    builder.add_edge("hitl_interrupt", END)

    # ── Conditional edges ─────────────────────────────────────────────────────
    builder.add_conditional_edges(
        source="evaluator",
        path=route_after_evaluator,
        path_map={
            "calculator": "calculator",
            "extractor": "extractor",
            "hitl_interrupt": "hitl_interrupt",
        },
    )
    builder.add_conditional_edges(
        source="delivery",
        path=route_after_delivery,
        path_map={
            "delivery": "delivery",
            "__end__": END,
        },
    )

    compiled = builder.compile(checkpointer=checkpointer)
    log.info("graph.compiled", checkpointer=type(checkpointer).__name__)
    return compiled
