"""
core/state.py
-------------
Single source of truth for the LangGraph shared state schema.
Imported by graph.py and all agent nodes — never import graph.py from here.
"""

from __future__ import annotations

import operator
from typing import Annotated, Dict, List, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class LeadInfo(BaseModel):
    """Immutable lead payload received from the API layer."""

    id: str = Field(..., description="Unique lead identifier (UUID or CRM ref).")
    raw_text: str = Field(..., description="Unstructured input text from the lead.")
    tenant_id: str = Field(..., description="Tenant identifier — scopes ChromaDB collection to catalogue_{tenant_id}.")


class LeadState(TypedDict):
    """
    Shared mutable state threaded through every LangGraph node.

    Notes
    -----
    - ``sse_logs`` uses ``operator.add`` as its reducer so that each node
      *appends* to the list rather than replacing it (fan-in safe).
    - All other fields are last-write-wins (standard LangGraph default).
    """

    lead_info: LeadInfo
    """Original (immutable) lead data."""

    sanitized_text: str
    """PII-masked version of raw_text produced by SanitizerNode."""

    extracted_services: List[str]
    """Service names / keywords extracted by ExtractorNode (LLM output)."""

    mapped_services: List[Dict]
    """
    Price-list entries returned by MapperNode (ChromaDB lookup).
    Each dict must contain at least ``{"service": str, "price": float}``.
    """

    total_quote: float
    """Final summed quote computed by CalculatorNode (pure Python, no LLM)."""

    retry_count: int
    """Number of Extractor→Mapper retry iterations performed so far."""

    sse_logs: Annotated[List[str], operator.add]
    """
    Append-only list of human-readable log lines streamed to the operator
    via Server-Sent Events.  Uses operator.add so nodes can safely append
    without clobbering concurrent writes.
    """

    error: Optional[str]
    """Optional error message set when a node catches an unrecoverable exception."""

    # ── Delivery fields (Fase 3) ──────────────────────────────────────────────

    delivery_status: str
    """
    Lifecycle status written exclusively by delivery_node.
    Values: "PENDING" (initial) | "SUCCESS" | "FAILED".
    Read by ``route_after_delivery`` to decide retry vs. END.
    """

    delivery_attempts: int
    """
    Number of delivery attempts performed so far.
    Incremented by delivery_node at the start of each attempt.
    Read by ``route_after_delivery`` to enforce the max-retry cap.
    Default: 0 (set in _make_initial_state).
    """

    delivery_error: Optional[str]
    """
    Human-readable error message from the last failed delivery attempt.
    None on success or before any attempt.  Surfaced in admin logs and
    the final SSE ``done`` frame.
    """
