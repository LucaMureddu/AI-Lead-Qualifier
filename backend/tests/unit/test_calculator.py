"""
tests/unit/test_calculator.py
-----------------------------
Unit test puri per ``_sum_prices`` e ``calculator_node`` (zero LLM, zero I/O).
"""

from __future__ import annotations

import pytest

from agents.calculator import _sum_prices, calculator_node

pytestmark = pytest.mark.unit


class TestSumPrices:
    def test_sums_correctly(self) -> None:
        assert _sum_prices([{"price": 100.0}, {"price": 250.50}, {"price": 49.99}]) == 400.49

    def test_empty_list_returns_zero(self) -> None:
        assert _sum_prices([]) == 0.0

    def test_rounding(self) -> None:
        # trappola float 0.1 + 0.2: deve dare 0.30, non 0.30000000000000004
        assert _sum_prices([{"price": 0.1}, {"price": 0.2}]) == 0.30

    def test_missing_price_key_raises(self) -> None:
        with pytest.raises(KeyError):
            _sum_prices([{"service": "X", "no_price": 0}])

    def test_numeric_string_is_cast(self) -> None:
        assert _sum_prices([{"price": "10.5"}, {"price": 1}]) == 11.5

    def test_non_numeric_price_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _sum_prices([{"price": "abc"}])

    def test_on_request_excluded_from_sum(self) -> None:
        """Un servizio is_on_request=True (price=0.0 nel DB) non deve entrare nel totale."""
        services = [
            {"price": 500.0, "is_on_request": False},
            {"price": 0.0,   "is_on_request": True},   # su richiesta
        ]
        assert _sum_prices(services) == 500.0

    def test_free_service_included_in_sum(self) -> None:
        """Un servizio legittimamente gratuito (price=0.0, is_on_request=False) contribuisce 0."""
        services = [
            {"price": 100.0, "is_on_request": False},
            {"price": 0.0,   "is_on_request": False},  # Gratis
        ]
        assert _sum_prices(services) == 100.0


class TestCalculatorNode:
    def test_total_quote_written(self, make_lead_state) -> None:
        state = make_lead_state(mapped_services=[
            {"matched_name": "SEO Audit", "price": 500.0, "is_on_request": False, "unit": "€"},
            {"matched_name": "Web Dev",   "price": 2000.0, "is_on_request": False, "unit": "€"},
        ])
        assert calculator_node(state)["total_quote"] == 2500.0

    def test_empty_services_returns_zero(self, make_lead_state) -> None:
        assert calculator_node(make_lead_state(mapped_services=[]))["total_quote"] == 0.0

    def test_malformed_service_sets_error(self, make_lead_state) -> None:
        result = calculator_node(make_lead_state(mapped_services=[{"service": "X"}]))  # manca "price"
        assert result.get("error_detail") is not None
        assert result["total_quote"] == 0.0

    def test_on_request_uses_flag_not_price(self, make_lead_state) -> None:
        """is_on_request=True deve finire in on_request_services anche se price != 0."""
        state = make_lead_state(mapped_services=[
            {"matched_name": "Consulenza", "price": 0.0,   "is_on_request": True},
            {"matched_name": "Hosting",    "price": 50.0,  "is_on_request": False},
        ])
        result = calculator_node(state)
        assert result["on_request_services"] == ["Consulenza"]
        assert result["total_quote"] == 50.0

    def test_free_service_not_in_on_request(self, make_lead_state) -> None:
        """Un servizio Gratis (price=0.0, is_on_request=False) NON deve finire in on_request."""
        state = make_lead_state(mapped_services=[
            {"matched_name": "Onboarding", "price": 0.0, "is_on_request": False},
            {"matched_name": "Setup",      "price": 200.0, "is_on_request": False},
        ])
        result = calculator_node(state)
        assert result["on_request_services"] == []
        assert result["total_quote"] == 200.0
