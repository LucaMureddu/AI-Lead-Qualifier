"""
tests/unit/test_agent_timeouts.py
-----------------------------------
Verifica il comportamento del sistema sotto stress e in scenari di timeout/failure.

Due categorie di errori con semantica diversa nell'architettura:

  1. Errori infrastrutturali (network, timeout LLM, HTTP 5xx):
     → extractor_node solleva RuntimeError
     → ARQ retries the job (il retry_count di LangGraph NON viene consumato)
     → Questo è corretto: non usare un slot di retry per un fault infrastrutturale

  2. Errori logici (LLM risponde ma il JSON è malformato o la lista è vuota):
     → extractor_node ritorna e incrementa retry_count
     → Il grafo decide se riprovare o escalare a HITL

Testare anche il boundary check di route_after_evaluator che determina
se il loop extractor→evaluator può diventare infinito.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.extractor import extractor_node
from core.graph import route_after_delivery, route_after_evaluator

pytestmark = pytest.mark.unit


# ═════════════════════════════════════════════════════════════════════════════
# 1. Errori infrastrutturali — sollevano RuntimeError (NO retry_count++)
# ═════════════════════════════════════════════════════════════════════════════

class TestInfrastructureErrors:
    """
    Gli errori infrastrutturali (network, timeout, HTTP 5xx) devono sollevare
    RuntimeError, non incrementare retry_count. Il retry è responsabilità di ARQ.
    """

    @pytest.mark.asyncio
    async def test_connect_error_raises_runtime_error(self, make_lead_state) -> None:
        """httpx.ConnectError → RuntimeError, non LogicalFailure."""
        state = make_lead_state(
            sanitized_text="Voglio un sito web.",
            retry_count=0,
        )
        with patch(
            "agents.extractor._call_openai_compatible",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            with pytest.raises(RuntimeError, match="LLM network error"):
                await extractor_node(state)

    @pytest.mark.asyncio
    async def test_timeout_raises_runtime_error(self, make_lead_state) -> None:
        """httpx.TimeoutException → RuntimeError, non retry interno."""
        state = make_lead_state(
            sanitized_text="Voglio un server email.",
            retry_count=0,
        )
        with patch(
            "agents.extractor._call_openai_compatible",
            side_effect=httpx.ConnectTimeout("read timeout"),
        ):
            with pytest.raises(RuntimeError, match="LLM network error"):
                await extractor_node(state)

    @pytest.mark.asyncio
    async def test_http_5xx_raises_runtime_error(self, make_lead_state) -> None:
        """HTTP 500 dall'endpoint LLM → RuntimeError (infrastrutturale)."""
        state = make_lead_state(
            sanitized_text="Consulenza cloud.",
            retry_count=0,
        )
        mock_response = MagicMock()
        mock_response.status_code = 500
        with patch(
            "agents.extractor._call_openai_compatible",
            side_effect=httpx.HTTPStatusError(
                "500 Internal Server Error",
                request=MagicMock(),
                response=mock_response,
            ),
        ):
            with pytest.raises(RuntimeError, match="LLM HTTP 500"):
                await extractor_node(state)

    @pytest.mark.asyncio
    async def test_infra_error_does_not_increment_retry_count(self, make_lead_state) -> None:
        """
        Critico: un errore infrastrutturale NON deve consumare retry_count.
        Se incrementasse retry_count, un cluster instabile esaurirebbe silenziosamente
        i retry LangGraph senza che l'LLM abbia MAI risposto.
        """
        state = make_lead_state(
            sanitized_text="Sito web urgente.",
            retry_count=1,
        )
        with patch(
            "agents.extractor._call_openai_compatible",
            side_effect=httpx.ConnectError("network down"),
        ):
            with pytest.raises(RuntimeError):
                await extractor_node(state)

        # Lo stato non viene modificato (RuntimeError viene propagata, nessun return)
        # → retry_count rimane 1 (ARQ riprenderà il job mantenendo lo stesso stato)
        assert state["retry_count"] == 1, (
            "Un errore infrastrutturale ha consumato retry_count. "
            "Il recovery da network failures verrà compromesso."
        )

    @pytest.mark.asyncio
    async def test_generic_exception_raises_runtime_error(self, make_lead_state) -> None:
        """Qualsiasi eccezione imprevista del provider → RuntimeError (non propagata raw)."""
        state = make_lead_state(
            sanitized_text="Progetto urgente.",
            retry_count=0,
        )
        with patch(
            "agents.extractor._call_openai_compatible",
            side_effect=Exception("SDK crash"),
        ):
            with pytest.raises(RuntimeError, match="LLM call failed"):
                await extractor_node(state)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Errori logici — incrementano retry_count (NON RuntimeError)
# ═════════════════════════════════════════════════════════════════════════════

class TestLogicalFailures:
    """
    Gli errori logici (LLM risponde ma il contenuto è inutile) NON devono
    sollevare eccezioni. Il nodo ritorna lo stato con retry_count++ e il
    router decide se riprovare o escalare a HITL.
    """

    @pytest.mark.asyncio
    async def test_empty_json_array_increments_retry_count(self, make_lead_state) -> None:
        """LLM ritorna '[]' → nessun servizio estratto → retry_count++."""
        state = make_lead_state(
            sanitized_text="Voglio qualcosa.",
            retry_count=0,
        )
        with patch("agents.extractor._call_openai_compatible", return_value="[]"):
            result = await extractor_node(state)

        assert result["extracted_services"] == []
        assert result["retry_count"] == 1  # Incrementato
        assert result["error_detail"] is not None  # Segnala il problema

    @pytest.mark.asyncio
    async def test_malformed_json_increments_retry_count(self, make_lead_state) -> None:
        """JSON malformato → fallback a lista vuota → retry_count++."""
        state = make_lead_state(
            sanitized_text="Migrazione dati.",
            retry_count=0,
        )
        with patch(
            "agents.extractor._call_openai_compatible",
            return_value="not valid json at all",
        ):
            result = await extractor_node(state)

        assert result["retry_count"] == 1
        assert result.get("error_detail") is not None

    @pytest.mark.asyncio
    async def test_successful_extraction_also_increments_retry_count(self, make_lead_state) -> None:
        """
        Anche un'estrazione riuscita incrementa retry_count (contatore di passaggi).
        Questo è il comportamento atteso: retry_count misura quante volte il ciclo
        extractor→evaluator è stato percorso.
        """
        state = make_lead_state(
            sanitized_text="Voglio un sito web e una app mobile.",
            retry_count=0,
        )
        with patch(
            "agents.extractor._call_openai_compatible",
            return_value='["Sito Web", "App Mobile"]',
        ):
            result = await extractor_node(state)

        assert result["extracted_services"] == ["Sito Web", "App Mobile"]
        assert result["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_retry_count_accumulates_across_logical_failures(self, make_lead_state) -> None:
        """
        Due fallimenti logici consecutivi → retry_count arriva a 2.
        Simula il ciclo: extractor(fail) → evaluator → extractor(fail) → HITL.
        """
        # Primo passaggio
        state = make_lead_state(sanitized_text="Testo.", retry_count=0)
        with patch("agents.extractor._call_openai_compatible", return_value="[]"):
            result1 = await extractor_node(state)
        assert result1["retry_count"] == 1

        # LangGraph applica il merge: aggiorna lo stato con retry_count=1
        state["retry_count"] = result1["retry_count"]

        # Secondo passaggio
        with patch("agents.extractor._call_openai_compatible", return_value="[]"):
            result2 = await extractor_node(state)
        assert result2["retry_count"] == 2


# ═════════════════════════════════════════════════════════════════════════════
# 3. Loop boundary — il grafo non cicla all'infinito
# ═════════════════════════════════════════════════════════════════════════════

class TestNoInfiniteLoop:
    """
    Verifica che il routing route_after_evaluator smetta di mandare il grafo
    in extractor prima o poi, terminando in hitl_interrupt.

    Un loop infinito in produzione consuma crediti LLM illimitati e blocca
    il job ARQ per sempre.
    """

    def _simulate_loop(self, max_retry: int) -> list[str]:
        """
        Simula il ciclo extractor→evaluator con score sempre basso.
        Restituisce la lista di destinazioni del router a ogni step.
        """
        from core.config import get_settings

        settings = get_settings()

        destinations: list[str] = []
        retry_count = 0
        low_score = settings.evaluator_threshold - 0.01  # Sempre sotto la soglia

        while True:
            state = {
                "confidence_score": low_score,
                "retry_count": retry_count,
                "lead": MagicMock(),
                "delivery_status": "PENDING",
                "delivery_attempts": 0,
            }
            dest = route_after_evaluator(state)
            destinations.append(dest)

            if dest == "hitl_interrupt":
                break
            if dest == "calculator":
                break  # Non dovrebbe accadere con low_score
            if dest == "extractor":
                retry_count += 1  # Simuliamo l'incremento del retry_count

            # Safety: se il loop supera max_retry * 2, qualcosa è sbagliato
            if len(destinations) > max_retry * 2 + 10:
                break

        return destinations

    def test_loop_terminates_in_hitl_after_max_retries(self) -> None:
        """Il loop si ferma sempre in hitl_interrupt, mai cicla per sempre."""
        from core.config import get_settings
        settings = get_settings()

        destinations = self._simulate_loop(settings.max_retry_count)

        # L'ultima destinazione deve essere hitl_interrupt
        assert destinations[-1] == "hitl_interrupt", (
            f"Il loop non ha raggiunto hitl_interrupt: {destinations}"
        )

    def test_loop_passes_through_extractor_expected_times(self) -> None:
        """Il numero di passaggi in extractor deve essere esattamente max_retry_count."""
        from core.config import get_settings
        settings = get_settings()

        destinations = self._simulate_loop(settings.max_retry_count)
        extractor_hops = destinations.count("extractor")

        assert extractor_hops == settings.max_retry_count, (
            f"Loop ha percorso extractor {extractor_hops} volte, "
            f"atteso {settings.max_retry_count}."
        )

    def test_loop_never_contains_consecutive_hitl_interrupts(self) -> None:
        """
        hitl_interrupt deve comparire esattamente una volta come destinazione finale.
        Se comparisse prima (mentre ci sono ancora retry), sarebbe un bug di routing.
        """
        from core.config import get_settings
        settings = get_settings()

        destinations = self._simulate_loop(settings.max_retry_count)
        hitl_count = destinations.count("hitl_interrupt")

        assert hitl_count == 1, (
            f"hitl_interrupt è apparso {hitl_count} volte: {destinations}"
        )

    def test_high_confidence_exits_loop_immediately(self, make_lead_state) -> None:
        """Con score sopra threshold, il grafo esce dal loop immediatamente (no retry)."""
        from core.config import get_settings
        settings = get_settings()

        state = make_lead_state(
            confidence_score=settings.evaluator_threshold + 0.1,
            retry_count=0,
        )
        dest = route_after_evaluator(state)
        assert dest == "calculator", (
            "Score sopra threshold deve andare direttamente al calculator, "
            "non fare alcun retry."
        )


# ═════════════════════════════════════════════════════════════════════════════
# 4. Delivery retry loop — route_after_delivery non cicla per sempre
# ═════════════════════════════════════════════════════════════════════════════

class TestDeliveryRetryLoop:
    """
    Verifica che il loop di retry della delivery si arresti a delivery_max_attempts.
    Un loop infinito su delivery consuma email/webhook slots e blocca il job ARQ.
    """

    def test_delivery_loop_terminates_after_max_attempts(self, make_lead_state) -> None:
        """
        Simula il loop: delivery(FAILED) → delivery → ... → END.
        """
        from core.config import get_settings
        settings = get_settings()

        destinations: list[str] = []
        attempts = 0

        while True:
            state = make_lead_state(
                delivery_status="FAILED",
                delivery_attempts=attempts,
            )
            dest = route_after_delivery(state)
            destinations.append(dest)

            if dest == "__end__":
                break
            if dest == "delivery":
                attempts += 1

            if len(destinations) > settings.delivery_max_attempts * 2 + 5:
                break

        assert destinations[-1] == "__end__"

    def test_delivery_max_attempts_is_respected(self, make_lead_state) -> None:
        """delivery_attempts == delivery_max_attempts → END (non un altro retry)."""
        from core.config import get_settings
        settings = get_settings()

        state = make_lead_state(
            delivery_status="FAILED",
            delivery_attempts=settings.delivery_max_attempts,
        )
        assert route_after_delivery(state) == "__end__"

    def test_delivery_success_exits_immediately(self, make_lead_state) -> None:
        """SUCCESS alla prima chiamata → END senza retry."""
        state = make_lead_state(delivery_status="SUCCESS", delivery_attempts=1)
        assert route_after_delivery(state) == "__end__"


# ═════════════════════════════════════════════════════════════════════════════
# 5. Short-circuit — extractor con sanitized_text vuoto
# ═════════════════════════════════════════════════════════════════════════════

class TestExtractorShortCircuit:
    """
    Se sanitized_text è vuoto, l'extractor deve uscire subito senza chiamare
    l'LLM. Questo previene sia sprechi di crediti che fughe di PII (chiamare
    l'LLM con testo grezzo non sanitizzato).
    """

    @pytest.mark.asyncio
    async def test_empty_sanitized_text_no_llm_call(self, make_lead_state) -> None:
        state = make_lead_state(sanitized_text="", retry_count=0)

        llm_called = False

        async def _mock_llm(system: str, user: str) -> str:
            nonlocal llm_called
            llm_called = True
            return "[]"

        with patch("agents.extractor._call_openai_compatible", new=_mock_llm):
            result = await extractor_node(state)

        assert not llm_called
        assert result["extracted_services"] == []
        # Non consuma retry_count (early exit non è un fallimento logico del LLM)
        assert "retry_count" not in result or result.get("retry_count", 0) == 0
