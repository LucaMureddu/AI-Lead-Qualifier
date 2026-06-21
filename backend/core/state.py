"""
core/state.py
-------------
Single source of truth for the LangGraph shared state schema — V2.

V2 changes vs V1
----------------
- LeadInfo → LeadContext (lead_id instead of id, raw_payload dict instead of raw_text)
- LeadState → AgentState (new fields: messages, retrieved_docs, confidence_score, HITL, status)
- sse_logs REMOVED (replaced by Postgres polling + structlog)
- error → error_detail (renamed for clarity)
"""

from __future__ import annotations

import operator
from typing import Annotated, Dict, List, Optional

from langchain_core.documents import Document
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class LeadContext(BaseModel):
    """Immutable lead payload written by the API Ingestion layer. Read by all nodes."""

    lead_id: str = Field(..., description="Unique lead identifier (UUID or CRM ref).")
    tenant_id: str = Field(..., description="Tenant identifier — scopes pgvector queries.")
    raw_payload: Dict = Field(
        ...,
        description="Raw text + optional CRM metadata. Key 'text' holds the lead body.",
    )
    metadata: Dict = Field(default_factory=dict, description="Optional CRM metadata.")


class AgentState(TypedDict):
    """
    Shared mutable state threaded through every LangGraph node — V2.

    Notes
    -----
    - ``messages`` uses ``operator.add`` as its reducer so nodes *append*
      rather than replace (fan-in safe).
    - All other fields are last-write-wins (standard LangGraph default).
    """

    # ── Initial Data ───────────────────────────────────────────────────────────
    lead: LeadContext
    """Written by: API Ingestion. Read by: all nodes."""

    # ── LLM Conversational State ───────────────────────────────────────────────
    messages: Annotated[List[BaseMessage], operator.add]
    """Append-only message history (fan-in safe via operator.add)."""

    # ── Multitenant RAG ────────────────────────────────────────────────────────
    retrieved_docs: List[Document]
    """Written by: MapperNode (pgvector). Read by: EvaluatorNode."""

    # ── Flow Control & HITL ───────────────────────────────────────────────────
    confidence_score: float
    """Written by: EvaluatorNode. Read by: Router. Range [0.0, 1.0]."""

    human_approved: Optional[bool]
    """Written by: /approve endpoint. None = not yet reviewed."""

    review_feedback: Optional[str]
    """Written by: UI (if rejected). Injected into extractor prompt on resume."""

    status: str
    """Lifecycle: 'queued'|'processing'|'pending_review'|'completed'|'error'."""

    error_detail: Optional[str]
    """Written by: fallback handlers. Surfaced via /status polling."""

    # ── Final Output (adapted from V1) ────────────────────────────────────────
    sanitized_text: str
    """PII-masked version of raw_payload['text'] produced by SanitizerNode."""

    extracted_services: List[str]
    """Service names extracted by ExtractorNode (LLM output)."""

    mapped_services: List[Dict]
    """Price-list entries returned by MapperNode (pgvector lookup).

    Contratto V3 — ogni dict contiene almeno:
      {'service': str, 'price': float | None, 'price_type': str}

    price_type è la proiezione serializzata della colonna tipizzata di prima
    classe introdotta in V3. I nodi downstream lo leggono come:
      entry["price_type"] == "VARIABLE"  →  servizio su richiesta / da preventivare
      entry["price_type"] == "FREE"      →  servizio gratuito (price = 0.0)
      entry["price_type"] == "FIXED"     →  prezzo fisso noto (price IS NOT NULL)

    La property ServiceItem.is_computable è la fonte canonica dell'informazione;
    il check su price_type == 'VARIABLE' nei dict ne è la proiezione."""

    total_quote: float
    """Final summed quote computed by CalculatorNode (pure Python, no LLM)."""

    on_request_services: List[str]
    """Service names with price_type=VARIABLE (da preventivare). Excluded from total_quote."""

    retry_count: int
    """Number of Extractor→Mapper retry iterations performed so far."""

    # ── Delivery fields ────────────────────────────────────────────────────────
    delivery_status: str
    """Lifecycle: 'PENDING'|'SUCCESS'|'FAILED'. Written by DeliveryNode."""

    delivery_attempts: int
    """Number of delivery attempts. Incremented by DeliveryNode."""

    delivery_error: Optional[str]
    """Last delivery error message. None on success."""
