"""
agents/evaluator.py
-------------------
EvaluatorNode — NEW in V2.

Calculates the confidence_score after MapperNode and decides whether the
result is good enough to proceed to CalculatorNode or needs human review.

Score formula
-------------
    mapped_ratio  = min(len(mapped_services) / max(len(extracted_services), 1), 1.0)
    avg_distance  = mean cosine distance of retrieved_docs (lower = better match)
    score         = mapped_ratio * (1.0 - min(avg_distance, 1.0))
    score         = clamp(score, 0.0, 1.0)

    The mapped_ratio cap at 1.0 is critical: pgvector returns k nearest
    neighbours per query, so if k=3 and extracted_services=1, the raw ratio
    would be 3.0 — making score exceed 1.0 and falsely bypassing the HITL
    threshold.  Capping at 1.0 makes the ratio represent coverage (did we
    find at least one match per requested service?), not raw result count.

    Hard-zero conditions (V2.1 fix):
    - No extracted_services  → score = 0.0 (nothing to qualify)
    - No mapped_services     → score = 0.0 (mapper produced no results)
    - No retrieved_docs      → score = 0.0 (no vector store evidence;
                               previously fell back to mapped_ratio, which
                               produced a false positive when the catalogue
                               is empty — fixed in V2.1)

    V2.1 rationale for the retrieved_docs hard-zero:
    The old fallback ``score = mapped_ratio`` was unsound: if retrieved_docs
    is empty the vector store returned nothing (empty catalogue, embedding
    failure, or dimension mismatch). Trusting mapped_ratio in that case
    means we'd approve a quote with zero evidence from the catalogue. The
    correct behaviour is to score 0.0 and let the retry / HITL logic decide.

Range: [0.0, 1.0]. The router in core/graph.py routes to calculator if
score >= 0.75, otherwise to extractor (retry) or hitl_interrupt (if retries
exhausted).

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


async def evaluator_node(state: AgentState) -> Dict:
    """
    LangGraph node: compute confidence_score from mapper results.

    Reads: extracted_services, mapped_services, retrieved_docs
    Writes: confidence_score, (optionally) status
    """
    lead_id: str = state["lead"].lead_id
    tenant_id: str = state["lead"].tenant_id
    extracted: list = state.get("extracted_services", [])
    mapped: list = state.get("mapped_services", [])
    retrieved: list = state.get("retrieved_docs", [])

    score: float
    reason: str

    if not extracted:
        # Nothing was extracted — no basis to qualify.
        score = 0.0
        reason = "no_extracted_services"
    elif not mapped:
        # Mapper produced no results — catalogue miss or embedding failure.
        # Hard zero: we must not proceed to CalculatorNode with empty mapping.
        score = 0.0
        reason = "no_mapped_services"
    elif not retrieved:
        # Vector store returned no documents — empty catalogue, embedding
        # dimension mismatch, or the mapper skipped the distance lookup.
        # Hard zero: without retrieval evidence we cannot assess quality.
        # (V2.1 fix: the old fallback to mapped_ratio was a false positive.)
        score = 0.0
        reason = "no_retrieved_docs"
    else:
        avg_distance: float = sum(
            d.metadata.get("distance", 1.0) for d in retrieved
        ) / len(retrieved)
        # mapped_ratio: capped at 1.0 so that k nearest-neighbour results
        # (k=3 per query) never inflate the ratio above full coverage.
        # Example of the pre-fix bug: extracted=1, mapped=3 → ratio=3.0,
        # score=3.0, HITL threshold bypassed silently.
        mapped_ratio: float = min(len(mapped) / max(len(extracted), 1), 1.0)
        # High coverage + low cosine distance = high confidence.
        raw_score: float = mapped_ratio * (1.0 - min(avg_distance, 1.0))
        # Final clamp: guard against any floating-point edge case.
        score = round(min(max(raw_score, 0.0), 1.0), 4)
        reason = "computed"

    settings = get_settings()
    threshold: float = settings.evaluator_threshold
    retry: int = state.get("retry_count", 0)
    heading_to_hitl: bool = (
        score < threshold and retry >= settings.max_retry_count
    )

    log.info(
        "evaluator.scored",
        lead_id=lead_id,
        tenant_id=tenant_id,
        confidence=round(score, 3),
        extracted=len(extracted),
        mapped=len(mapped),
        retrieved_docs=len(retrieved),
        threshold=threshold,
        passes=score >= threshold,
        reason=reason,
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
