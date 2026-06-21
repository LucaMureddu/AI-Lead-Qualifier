"""
tests/unit/test_calculator.py
-----------------------------
Unit test puri per ``_sum_prices`` e ``calculator_node`` (zero LLM, zero I/O).

V3: i fixture usano price_type invece di is_on_request.
    VARIABLE items hanno price=None (non più 0.0 sentinel).
"""

from __future__ import annotations

import pytest

from agents.calculator import _sum_prices, calculator_node

pytestmark = pytest.mark.unit


class TestSumPrices:
    def test_sums_correctly(self) -> None:
        services = [
            {"price": 100.0,  "price_type": "FIXED"},
            {"price": 250.50, "price_type": "FIXED"},
            {"price": 49.99,  "price_type": "FIXED"},
        ]
        assert _sum_prices(services) == 400.49

    def test_empty_list_returns_zero(self) -> None:
        assert _sum_prices([]) == 0.0

    def test_rounding(self) -> None:
        # trappola float 0.1 + 0.2: deve dare 0.30, non 0.30000000000000004
        services = [
            {"price": 0.1, "price_type": "FIXED"},
            {"price": 0.2, "price_type": "FIXED"},
        ]
        assert _sum_prices(services) == 0.30

    def test_missing_price_key_raises_on_fixed(self) -> None:
        """Un entry FIXED senza chiave 'price' deve sollevare KeyError."""
        with pytest.raises(KeyError):
            _sum_prices([{"service": "X", "price_type": "FIXED"}])

    def test_numeric_string_is_cast(self) -> None:
        services = [
            {"price": "10.5", "price_type": "FIXED"},
            {"price": 1,      "price_type": "FIXED"},
        ]
        assert _sum_prices(services) == 11.5

    def test_non_numeric_price_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _sum_prices([{"price": "abc", "price_type": "FIXED"}])

    def test_variable_excluded_from_sum(self) -> None:
        """VARIABLE (price=None) non deve entrare nel totale — e non deve sollevare TypeError."""
        services = [
            {"price": 500.0, "price_type": "FIXED"},
            {"price": None,  "price_type": "VARIABLE"},  # da preventivare
        ]
        assert _sum_prices(services) == 500.0

    def test_free_included_in_sum_as_zero(self) -> None:
        """FREE (price=0.0) contribuisce 0 al totale e NON è su richiesta."""
        services = [
            {"price": 100.0, "price_type": "FIXED"},
            {"price": 0.0,   "price_type": "FREE"},   # Gratis
        ]
        assert _sum_prices(services) == 100.0

    def test_mathematical_invariance(self) -> None:
        """total == somma dei soli FIXED + FREE, VARIABLE escluso."""
        services = [
            {"price": 200.0, "price_type": "FIXED"},
            {"price": 0.0,   "price_type": "FREE"},
            {"price": None,  "price_type": "VARIABLE"},
            {"price": 50.0,  "price_type": "FIXED"},
        ]
        assert _sum_prices(services) == 250.0


class TestCalculatorNode:
    def test_total_quote_written(self, make_lead_state) -> None:
        state = make_lead_state(mapped_services=[
            {"matched_name": "SEO Audit", "price": 500.0,  "price_type": "FIXED", "unit": "€"},
            {"matched_name": "Web Dev",   "price": 2000.0, "price_type": "FIXED", "unit": "€"},
        ])
        assert calculator_node(state)["total_quote"] == 2500.0

    def test_empty_services_returns_zero(self, make_lead_state) -> None:
        assert calculator_node(make_lead_state(mapped_services=[]))["total_quote"] == 0.0

    def test_malformed_fixed_service_sets_error(self, make_lead_state) -> None:
        """Un entry FIXED senza 'price' deve produrre error_detail, non crash."""
        result = calculator_node(
            make_lead_state(mapped_services=[{"service": "X", "price_type": "FIXED"}])
        )
        assert result.get("error_detail") is not None
        assert result["total_quote"] == 0.0

    def test_variable_goes_to_on_request_not_total(self, make_lead_state) -> None:
        """VARIABLE (price=None) finisce in on_request_services, non nel totale."""
        state = make_lead_state(mapped_services=[
            {"matched_name": "Consulenza", "price": None,  "price_type": "VARIABLE"},
            {"matched_name": "Hosting",    "price": 50.0,  "price_type": "FIXED"},
        ])
        result = calculator_node(state)
        assert result["on_request_services"] == ["Consulenza"]
        assert result["total_quote"] == 50.0

    def test_free_not_in_on_request(self, make_lead_state) -> None:
        """FREE (price=0.0) NON deve finire in on_request_services."""
        state = make_lead_state(mapped_services=[
            {"matched_name": "Onboarding", "price": 0.0,   "price_type": "FREE"},
            {"matched_name": "Setup",      "price": 200.0, "price_type": "FIXED"},
        ])
        result = calculator_node(state)
        assert result["on_request_services"] == []
        assert result["total_quote"] == 200.0

    def test_variable_price_none_does_not_raise(self, make_lead_state) -> None:
        """float(None) non viene mai chiamato su un entry VARIABLE."""
        state = make_lead_state(mapped_services=[
            {"matched_name": "X", "price": None, "price_type": "VARIABLE"},
        ])
        result = calculator_node(state)
        assert result["total_quote"] == 0.0
        assert result["on_request_services"] == ["X"]
        assert result.get("error_detail") is None
