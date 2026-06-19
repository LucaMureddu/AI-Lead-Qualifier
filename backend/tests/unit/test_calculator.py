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


class TestCalculatorNode:
    def test_total_quote_written(self, make_lead_state) -> None:
        state = make_lead_state(mapped_services=[
            {"matched_name": "SEO Audit", "price": 500.0, "unit": "€"},
            {"matched_name": "Web Dev", "price": 2000.0, "unit": "€"},
        ])
        assert calculator_node(state)["total_quote"] == 2500.0

    def test_empty_services_returns_zero(self, make_lead_state) -> None:
        assert calculator_node(make_lead_state(mapped_services=[]))["total_quote"] == 0.0

    def test_malformed_service_sets_error(self, make_lead_state) -> None:
        result = calculator_node(make_lead_state(mapped_services=[{"service": "X"}]))  # manca "price"
        assert result.get("error_detail") is not None
        assert result["total_quote"] == 0.0
