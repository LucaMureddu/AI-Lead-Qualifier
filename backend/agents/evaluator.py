"""
agents/evaluator.py
-------------------
EvaluatorNode — NEW in V2.

Calculates the confidence_score after MapperNode and decides whether the
result is good enough to proceed to CalculatorNode or needs human review.

Score formula
-------------
    mapped_ratio  = len(mapped_services) / len(extracted_services)
    avg_distance  = mean cosine distance of retrieved_docs (lower = better match)
    score         = mapped_ratio * (1.0 - min(avg_distance, 1.0))

    Special cases:
    - No extracted_services  → score = 0.0 (nothing to qualify)
    - No retrieved_docs      → score = mapped_ratio (distance factor omitted;
                               the mapper found matches but returned no doc
                               metadata, so we trust the coverage signal alone)

Range: [0.0, 1.0]. The router in core/graph.py routes to calculator if score >= 0.75,
otherwise to extractor (retry) or hitl_interrupt (if retries exhausted).

Audit logging
-------------
Only lead_id, tenant_id, node name, and derived metrics are logged — no PII.
"""

from __future__ import annotations

from typing import Dict

import structlog

from core.config import get_settings
from core.state import AgentState

log = structlog.get_logger()

CONFIDENCE_THRESHOLD = 0.75


async def evaluator_node(state: AgentState) -> Dict:
    """
    LangGraph node: compute confidence_score from mapper results.

    Reads: extracted_services, mapped_services, retrieved_docs
    Writes: confidence_score
    """
    lead_id: str = state["lead"].lead_id
    tenant_id: str = state["lead"].tenant_id
    extracted = state.get("extracted_services", [])
    mapped = state.get("mapped_services", [])
    retrieved = state.get("retrieved_docs", [])

    if not extracted:
        # Nothing was extracted — no basis to qualify.
        score = 0.0
    else:
        mapped_ratio = len(mapped) / len(extracted)
        if retrieved:
            avg_distance = sum(
                d.metadata.get("distance", 1.0) for d in retrieved
            ) / len(retrieved)
            # High coverage + low cosine distance = high confidence.
            score = mapped_ratio * (1.0 - min(avg_distance, 1.0))
        else:
            # No retrieved_docs means the vector store returned no metadata
            # (e.g. empty catalogue or mapper skipped the distance lookup).
            # We cannot penalise with avg_distance=1.0 (that would always
            # force HITL even when the mapper found good matches).
            # Fall back to mapped_ratio alone as the confidence signal.
            score = mapped_ratio

    settings = get_settings()
    retry: int = state.get("retry_count", 0)
    heading_to_hitl: bool = score < CONFIDENCE_THRESHOLD and retry >= settings.max_retry_count

    log.info(
        "evaluator.scored",
        lead_id=lead_id,
        tenant_id=tenant_id,
        confidence=round(score, 3),
        extracted=len(extracted),
        mapped=len(mapped),
        retrieved_docs=len(retrieved),
        threshold=CONFIDENCE_THRESHOLD,
        passes=score >= CONFIDENCE_THRESHOLD,
    )

    result: Dict = {"confidence_score": score}
    if heading_to_hitl:
        # Set status BEFORE hitl_interrupt_node calls interrupt().
        # interrupt() suspends the graph before the node returns, so any
        # state update inside hitl_interrupt_node is dead code — the checkpoint
        # would retain status="processing". Writing it here ensures Postgres
        # already has status="pending_review" when /status is polled and
        # /approve checks the state.
        result["status"] = "pending_review"

    return result
