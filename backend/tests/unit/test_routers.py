"""
tests/unit/test_routers.py
--------------------------
Matrice decisionale completa dei router (funzioni pure state -> stringa).
Coprono tutta la logica di retry/fallback/HITL senza eseguire il grafo.
"""

from __future__ import annotations

import pytest

from core.graph import route_after_delivery, route_after_mapper
from ingestion.graph import (
    route_after_approval,
    route_after_normalizer,
    route_after_validator,
)

pytestmark = pytest.mark.unit


# ── Qualify: route_after_mapper ────────────────────────────────────────────────

class TestRouteAfterMapper:
    def test_mapped_ok_to_calculator(self, make_lead_state) -> None:
        state = make_lead_state(mapped_services=[{"service": "X", "price": 100.0}])
        assert route_after_mapper(state) == "calculator"

    def test_empty_with_retries_left_to_extractor(self, make_lead_state) -> None:
        state = make_lead_state(mapped_services=[], retry_count=0)
        assert route_after_mapper(state) == "extractor"

    def test_empty_retries_exhausted_to_human_fallback(self, make_lead_state) -> None:
        state = make_lead_state(mapped_services=[], retry_count=2)  # == max_retry_count
        assert route_after_mapper(state) == "human_fallback"


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
