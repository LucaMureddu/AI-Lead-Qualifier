"""
tests/unit/test_routers.py
--------------------------
Matrice decisionale completa dei router (funzioni pure state -> stringa).
Coprono tutta la logica di retry/fallback/HITL senza eseguire il grafo.

NOTE: TestRouteAfterMapper rimosso in V2.1.
route_after_mapper è stata eliminata da core/graph.py perché in V2 l'edge
mapper → evaluator è statico. Tutta la logica di retry/HITL è delegata a
route_after_evaluator (coperta da TestRouteAfterEvaluator in test_nodes_qualify.py).
"""

from __future__ import annotations

import pytest

from core.graph import route_after_delivery
from ingestion.graph import (
    route_after_approval,
    route_after_normalizer,
    route_after_validator,
)

pytestmark = pytest.mark.unit


# ── Qualify: route_after_delivery ──────────────────────────────────────────────

class TestRouteAfterDelivery:
    def test_success_ends(self, make_lead_state) -> None:
        state = make_lead_state(delivery_status="SUCCESS", delivery_attempts=1)
        assert route_after_delivery(state) == "__end__"

    def test_failed_with_attempts_left_retries(self, make_lead_state) -> None:
        state = make_lead_state(delivery_status="FAILED", delivery_attempts=1)
        assert route_after_delivery(state) == "delivery"

    def test_failed_attempts_exhausted_ends(self, make_lead_state) -> None:
        state = make_lead_state(delivery_status="FAILED", delivery_attempts=3)  # == max
        assert route_after_delivery(state) == "__end__"


# ── Ingestion routers ──────────────────────────────────────────────────────────

def _ing(**ovr) -> dict:
    base = {
        "raw_chunks": [],
        "current_chunk_index": 0,
        "confidence_score": 1.0,
        "flagged_items": [],
        "approved": None,
    }
    base.update(ovr)
    return base


class TestRouteAfterNormalizer:
    def test_more_chunks_loops(self) -> None:
        state = _ing(raw_chunks=[[{}], [{}]], current_chunk_index=1)
        assert route_after_normalizer(state) == "normalizer"

    def test_all_chunks_done_to_validator(self) -> None:
        state = _ing(raw_chunks=[[{}]], current_chunk_index=1)
        assert route_after_normalizer(state) == "validator"


class TestRouteAfterValidator:
    def test_low_confidence_to_approval(self) -> None:
        assert route_after_validator(_ing(confidence_score=0.5, flagged_items=[])) == "approval"

    def test_flagged_item_to_approval(self) -> None:
        assert route_after_validator(_ing(confidence_score=1.0, flagged_items=[object()])) == "approval"

    def test_clean_to_finalizer(self) -> None:
        assert route_after_validator(_ing(confidence_score=0.9, flagged_items=[])) == "finalizer"


class TestRouteAfterApproval:
    def test_approved_to_finalizer(self) -> None:
        assert route_after_approval(_ing(approved=True)) == "finalizer"

    def test_rejected_to_end(self) -> None:
        assert route_after_approval(_ing(approved=False)) == "__end__"
