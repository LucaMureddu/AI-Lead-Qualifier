"""
core/graph.py
-------------
LangGraph orchestration layer.

Topology
--------
  SanitizerNode
       │
  ExtractorNode  ◄──────────────────────────────────────────┐
       │                                                     │ retry (retry_count < max)
  MapperNode ──── mapping_ok ──► CalculatorNode             │
       │                         │                          │
       │                    DeliveryNode ◄──────────────────┼── retry (delivery_attempts < max)
       │                         │                          │
       │                    SUCCESS → END                   │
       │                    FAILED & attempts >= max → END  │
       │                                                    │
       └──── mapping_failed & retry_count < max ───────────┘
       │
       └──── mapping_failed & retry_count == max ──► HumanFallbackNode

Persistence: AsyncSqliteSaver for async-native checkpointing (interrupt/resume support).
"""

from __future__ import annotations

import logging
from typing import Literal

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from agents.calculator import calculator_node
from agents.delivery import delivery_node
from agents.extractor import extractor_node
from agents.mapper import mapper_node
from agents.sanitizer import sanitizer_node
from core.config import get_settings
from core.state import LeadState

logger: logging.Logger = logging.getLogger(__name__)

# ── Routing helpers ───────────────────────────────────────────────────────────

def route_after_mapper(state: LeadState) -> Literal["calculator", "extractor", "human_fallback"]:
    """
    Conditional edge executed after MapperNode.

    Decision matrix:
    ┌──────────────────────────────┬───────────────────────────────────┐
    │ mapped_services non-empty    │ → CalculatorNode                  │
    │ empty & retry_count < max    │ → ExtractorNode (retry loop)      │
    │ empty & retry_count == max   │ → HumanFallbackNode               │
    └──────────────────────────────┴───────────────────────────────────┘
    """
    settings = get_settings()

    if len(state.get("mapped_services", [])) >= settings.mapper_min_results:
        logger.debug("route_after_mapper → calculator")
        return "calculator"

    retry_count: int = state.get("retry_count", 0)
    if retry_count < settings.max_retry_count:
        logger.debug("route_after_mapper → extractor (retry #%d)", retry_count)
        return "extractor"

    logger.warning("route_after_mapper → human_fallback (exhausted retries)")
    return "human_fallback"


# ── Delivery router ──────────────────────────────────────────────────────────

def route_after_delivery(state: LeadState) -> Literal["delivery", "__end__"]:
    """
    Conditional edge executed after DeliveryNode.

    Decision matrix:
    ┌─────────────────────────────────────────┬──────────────────────────────┐
    │ delivery_status == "SUCCESS"            │ → END                        │
    │ FAILED & delivery_attempts < max        │ → DeliveryNode (retry)       │
    │ FAILED & delivery_attempts >= max       │ → END (log abandonment)      │
    └─────────────────────────────────────────┴──────────────────────────────┘

    The ceiling is read from ``settings.delivery_max_attempts`` (default 3)
    so it can be tuned via environment variable without code changes.
    """
    settings = get_settings()
    status: str = state.get("delivery_status", "PENDING")
    attempts: int = state.get("delivery_attempts", 0)

    if status == "SUCCESS":
        logger.debug("route_after_delivery → END (success)")
        return "__end__"

    if attempts < settings.delivery_max_attempts:
        logger.warning(
            "route_after_delivery → delivery (retry #%d of %d)",
            attempts,
            settings.delivery_max_attempts,
        )
        return "delivery"

    logger.error(
        "route_after_delivery → END (abandoned after %d/%d attempts)",
        attempts,
        settings.delivery_max_attempts,
    )
    return "__end__"


# ── Human-fallback node ───────────────────────────────────────────────────────

def human_fallback_node(state: LeadState) -> LeadState:
    """
    Suspends graph execution via LangGraph interrupt().
    An operator can inspect the state and resume by calling the graph
    with an updated state (e.g. manually providing mapped_services).

    NOTE: interrupt() raises an internal LangGraph exception that signals
    the runtime to pause and persist the current checkpoint.
    """
    lead_id: str = state["lead_info"].id
    logger.warning("HumanFallbackNode activated for lead_id=%s", lead_id)

    sse_log_entry: str = (
        f"[HUMAN_FALLBACK] Lead {lead_id} requires manual review. "
        "Automatic mapping failed after max retries."
    )
    # interrupt() never returns; it suspends the graph at this node.
    interrupt(value={"lead_id": lead_id, "message": sse_log_entry})

    # Unreachable — satisfies type checker.
    return {**state, "sse_logs": [sse_log_entry]}  # type: ignore[return-value]


# ── Graph factory ─────────────────────────────────────────────────────────────

def build_graph(checkpointer: AsyncSqliteSaver | None = None):
    # NB: niente annotazione di ritorno esplicita: builder.compile() ritorna un
    # CompiledStateGraph (che espone ainvoke/astream), non uno StateGraph.
    # Lasciamo inferire mypy così i chiamanti (api/routes.py) hanno il tipo giusto.
    """
    Construct and compile the LangGraph StateGraph.

    Parameters
    ----------
    checkpointer:
        Optional AsyncSqliteSaver instance for checkpoint persistence.
        When provided, the graph supports interrupt/resume workflows.
        Pass ``None`` during unit tests to skip persistence.

    Returns
    -------
    CompiledGraph
        Ready-to-invoke compiled graph.
    """
    builder: StateGraph = StateGraph(LeadState)

    # ── Register nodes ────────────────────────────────────────────────────────
    builder.add_node("sanitizer", sanitizer_node)
    builder.add_node("extractor", extractor_node)
    builder.add_node("mapper", mapper_node)
    builder.add_node("calculator", calculator_node)
    builder.add_node("delivery", delivery_node)
    builder.add_node("human_fallback", human_fallback_node)

    # ── Static edges ──────────────────────────────────────────────────────────
    builder.set_entry_point("sanitizer")
    builder.add_edge("sanitizer", "extractor")
    builder.add_edge("extractor", "mapper")
    builder.add_edge("calculator", "delivery")
    builder.add_edge("human_fallback", END)

    # ── Conditional edges (routing logic) ────────────────────────────────────
    builder.add_conditional_edges(
        source="mapper",
        path=route_after_mapper,
        path_map={
            "calculator": "calculator",
            "extractor": "extractor",
            "human_fallback": "human_fallback",
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
    logger.info("LangGraph compiled successfully (checkpointer=%s)", type(checkpointer).__name__)
    return compiled


async def get_checkpointer() -> AsyncSqliteSaver:
    """
    Async factory that returns a ready-to-use ``AsyncSqliteSaver``.

    Why async?
    ----------
    ``AsyncSqliteSaver`` wraps an ``aiosqlite`` connection, which must be
    opened with ``await aiosqlite.connect(...)``.  The function therefore
    needs to be a coroutine so the caller can ``await get_checkpointer()``.

    Why not SqliteSaver?
    --------------------
    The sync ``SqliteSaver`` uses blocking ``sqlite3`` I/O and is incompatible
    with LangGraph's async graph methods (``astream``, ``ainvoke``): the
    internal checkpoint read/write calls would block the event loop and, in
    practice, raise a ``MissingImplementationError`` for the async abstract
    methods.

    Abstraction note: to switch to ``AsyncPostgresSaver`` for production,
    replace this function's body (import asyncpg / psycopg, open the async
    connection, return ``AsyncPostgresSaver(conn)``) without touching any
    other module.
    """
    settings = get_settings()
    db_path = settings.sqlite_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn: aiosqlite.Connection = await aiosqlite.connect(str(db_path))
    logger.info("AsyncSqliteSaver initialised at %s", db_path)
    return AsyncSqliteSaver(conn)
