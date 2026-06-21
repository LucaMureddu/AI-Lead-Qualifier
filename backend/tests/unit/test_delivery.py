"""
tests/unit/test_delivery.py
-----------------------------
Unit test per ``_format_quote_body`` e ``delivery_node`` (zero I/O, zero LLM).

V3: verifica i tre branch di price_type nel corpo dell'email:
  - VARIABLE → "• {nome} — su richiesta"
  - FREE     → "• {nome} — Gratis"
  - FIXED    → "• {nome} — {price:.2f} €"
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agents.delivery import _format_quote_body, delivery_node

pytestmark = pytest.mark.unit


# ═════════════════════════════════════════════════════════════════════════════
# _format_quote_body — V3 price_type branches
# ═════════════════════════════════════════════════════════════════════════════

class TestFormatQuoteBody:
    def _services(self, *items) -> list[dict]:
        """Costruisce una lista di servizi mappati per il test."""
        return list(items)

    def test_fixed_service_shows_price_with_two_decimals(self) -> None:
        services = [{"matched_name": "Sviluppo Sito", "price": 2500.0, "price_type": "FIXED"}]
        body = _format_quote_body(services, total_quote=2500.0, total_is_partial=False)
        assert "• Sviluppo Sito — 2500.00 €" in body

    def test_variable_service_shows_su_richiesta(self) -> None:
        """VARIABLE (price=None) → 'su richiesta', non un valore numerico."""
        services = [{"matched_name": "Consulenza Cloud", "price": None, "price_type": "VARIABLE"}]
        body = _format_quote_body(services, total_quote=0.0, total_is_partial=True)
        assert "• Consulenza Cloud — su richiesta" in body
        # La riga del servizio NON deve mostrare un valore numerico.
        # Nota: "0.00 €" può apparire nella riga del totale parziale (corretto),
        # quindi il controllo va fatto solo sulle righe dei servizi.
        assert "None €" not in body
        service_lines = [line for line in body.splitlines() if "Consulenza Cloud" in line]
        assert service_lines, "Riga servizio non trovata nel corpo"
        assert "0.00 €" not in service_lines[0]

    def test_free_service_shows_gratis(self) -> None:
        """FREE (price=0.0) → 'Gratis', non '0.00 €'."""
        services = [{"matched_name": "Onboarding", "price": 0.0, "price_type": "FREE"}]
        body = _format_quote_body(services, total_quote=0.0, total_is_partial=False)
        assert "• Onboarding — Gratis" in body
        assert "0.00 €" not in body.split("Riepilogo")[1].split("Totale")[0]

    def test_mixed_price_types(self) -> None:
        """Tre tipi di servizio nello stesso preventivo."""
        services = [
            {"matched_name": "Hosting",     "price": 120.0, "price_type": "FIXED"},
            {"matched_name": "Assistenza",  "price": None,  "price_type": "VARIABLE"},
            {"matched_name": "Formazione",  "price": 0.0,   "price_type": "FREE"},
        ]
        body = _format_quote_body(services, total_quote=120.0, total_is_partial=True)
        assert "• Hosting — 120.00 €" in body
        assert "• Assistenza — su richiesta" in body
        assert "• Formazione — Gratis" in body

    def test_total_line_when_no_variable(self) -> None:
        """Totale senza 'parziale' quando tutti i servizi sono FIXED o FREE."""
        services = [{"matched_name": "SEO", "price": 500.0, "price_type": "FIXED"}]
        body = _format_quote_body(services, total_quote=500.0, total_is_partial=False)
        assert "Totale: 500.00 €" in body
        assert "parziale" not in body

    def test_total_line_when_partial(self) -> None:
        """'Totale parziale' quando almeno un servizio è VARIABLE."""
        services = [
            {"matched_name": "Dev", "price": 1000.0, "price_type": "FIXED"},
            {"matched_name": "AI",  "price": None,   "price_type": "VARIABLE"},
        ]
        body = _format_quote_body(services, total_quote=1000.0, total_is_partial=True)
        assert "Totale parziale: 1000.00 €" in body
        assert "da preventivare" in body

    def test_missing_price_type_defaults_to_fixed(self) -> None:
        """Un entry senza price_type viene trattato come FIXED (retrocompatibilità)."""
        services = [{"matched_name": "Legacy", "price": 300.0}]
        body = _format_quote_body(services, total_quote=300.0, total_is_partial=False)
        assert "• Legacy — 300.00 €" in body

    def test_empty_services_list(self) -> None:
        """Lista servizi vuota → corpo valido senza righe servizio."""
        body = _format_quote_body([], total_quote=0.0, total_is_partial=False)
        assert "Riepilogo servizi" in body
        assert "Totale: 0.00 €" in body

    def test_matched_name_fallback_to_service_key(self) -> None:
        """Se matched_name è assente, usa il campo 'service' come fallback."""
        services = [{"service": "Migrazione DB", "price": 800.0, "price_type": "FIXED"}]
        body = _format_quote_body(services, total_quote=800.0, total_is_partial=False)
        assert "• Migrazione DB — 800.00 €" in body

    def test_price_formatted_with_two_decimal_places(self) -> None:
        """Il prezzo deve avere sempre esattamente due cifre decimali."""
        services = [{"matched_name": "Svc", "price": 99.9, "price_type": "FIXED"}]
        body = _format_quote_body(services, total_quote=99.9, total_is_partial=False)
        assert "99.90 €" in body


# ═════════════════════════════════════════════════════════════════════════════
# delivery_node — comportamento del nodo nel grafo
# ═════════════════════════════════════════════════════════════════════════════

class TestDeliveryNode:
    @pytest.mark.asyncio
    async def test_success_sets_status_completed(self, make_lead_state) -> None:
        """Delivery riuscita → delivery_status='SUCCESS', status='completed'."""
        state = make_lead_state(
            mapped_services=[{"matched_name": "Cloud", "price": 500.0, "price_type": "FIXED"}],
            total_quote=500.0,
        )
        with patch("agents.delivery.get_delivery_adapter") as mock_factory:
            adapter = AsyncMock()
            adapter.deliver = AsyncMock(return_value=True)
            mock_factory.return_value = adapter
            result = await delivery_node(state)

        assert result["delivery_status"] == "SUCCESS"
        assert result["status"] == "completed"
        assert result["delivery_error"] is None

    @pytest.mark.asyncio
    async def test_adapter_false_sets_failed(self, make_lead_state) -> None:
        """Adapter che ritorna False → FAILED, nessuna eccezione propagata."""
        state = make_lead_state(mapped_services=[])
        with patch("agents.delivery.get_delivery_adapter") as mock_factory:
            adapter = AsyncMock()
            adapter.deliver = AsyncMock(return_value=False)
            mock_factory.return_value = adapter
            result = await delivery_node(state)

        assert result["delivery_status"] == "FAILED"
        assert "status" not in result  # status non cambia su failure

    @pytest.mark.asyncio
    async def test_network_error_sets_failed(self, make_lead_state) -> None:
        """httpx.RequestError → FAILED, errore loggato, nessuna propagazione."""
        import httpx

        state = make_lead_state(mapped_services=[])
        with patch("agents.delivery.get_delivery_adapter") as mock_factory:
            adapter = AsyncMock()
            adapter.deliver = AsyncMock(
                side_effect=httpx.ConnectError("connection refused")
            )
            mock_factory.return_value = adapter
            result = await delivery_node(state)

        assert result["delivery_status"] == "FAILED"
        assert "Network error" in result["delivery_error"]

    @pytest.mark.asyncio
    async def test_unexpected_error_sets_failed(self, make_lead_state) -> None:
        """Eccezione generica → FAILED, non propagata."""
        state = make_lead_state(mapped_services=[])
        with patch("agents.delivery.get_delivery_adapter") as mock_factory:
            adapter = AsyncMock()
            adapter.deliver = AsyncMock(side_effect=ValueError("unexpected"))
            mock_factory.return_value = adapter
            result = await delivery_node(state)

        assert result["delivery_status"] == "FAILED"
        assert "Unexpected error" in result["delivery_error"]

    @pytest.mark.asyncio
    async def test_delivery_attempts_incremented(self, make_lead_state) -> None:
        """delivery_attempts deve essere incrementato ad ogni chiamata."""
        state = make_lead_state(mapped_services=[], delivery_attempts=2)
        with patch("agents.delivery.get_delivery_adapter") as mock_factory:
            adapter = AsyncMock()
            adapter.deliver = AsyncMock(return_value=True)
            mock_factory.return_value = adapter
            result = await delivery_node(state)

        assert result["delivery_attempts"] == 3

    @pytest.mark.asyncio
    async def test_variable_service_in_quote_body(self, make_lead_state) -> None:
        """V3: un servizio VARIABLE deve produrre 'su richiesta' nel quote_body."""
        services = [
            {"matched_name": "Consulenza", "price": None, "price_type": "VARIABLE"},
        ]
        state = make_lead_state(
            mapped_services=services,
            total_quote=0.0,
            on_request_services=["Consulenza"],
        )
        captured_payload: dict = {}

        with patch("agents.delivery.get_delivery_adapter") as mock_factory:
            adapter = AsyncMock()

            async def _capture(payload):
                captured_payload.update(payload)
                return True

            adapter.deliver = _capture
            mock_factory.return_value = adapter
            await delivery_node(state)

        assert "su richiesta" in captured_payload["quote_body"]
        assert captured_payload["total_is_partial"] is True

    @pytest.mark.asyncio
    async def test_free_service_in_quote_body(self, make_lead_state) -> None:
        """V3: un servizio FREE deve produrre 'Gratis' nel quote_body."""
        services = [
            {"matched_name": "Onboarding", "price": 0.0, "price_type": "FREE"},
        ]
        state = make_lead_state(
            mapped_services=services,
            total_quote=0.0,
            on_request_services=[],
        )
        captured_payload: dict = {}

        with patch("agents.delivery.get_delivery_adapter") as mock_factory:
            adapter = AsyncMock()

            async def _capture(payload):
                captured_payload.update(payload)
                return True

            adapter.deliver = _capture
            mock_factory.return_value = adapter
            await delivery_node(state)

        assert "Gratis" in captured_payload["quote_body"]
        assert captured_payload["total_is_partial"] is False
