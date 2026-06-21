"""
tests/unit/test_graph_state.py
--------------------------------
Verifica le corrette mutazioni del TypedDict AgentState tra i nodi del grafo.

Un Graph State mal progettato invalida l'intera architettura: un nodo che
scrive in un campo sbagliato o che sovrascrive dati di un altro nodo crea bug
silenziosi che emergono solo in staging o in produzione.

Cosa viene testato:
  - messages usa operator.add (append, non replace): test fondamentale per
    la semantica fan-in di LangGraph.
  - LeadContext è read-only per tutti i nodi: nessun nodo deve mutare il
    contesto di input (lead_id, tenant_id, raw_payload).
  - EvaluatorNode: hard-zero conditions, clamping [0,1], ratio cap.
  - Nodo fallito non corrompe i campi scritti dagli altri nodi.
  - route_after_evaluator e route_after_delivery: tutte le branch.
  - Downstream state isolation: il retry_count non viene azzerato da nodi che
    non lo toccano (campi preservati tra chiamate di nodo successive).
"""

from __future__ import annotations

import operator
from unittest.mock import MagicMock
import pytest

from agents.evaluator import evaluator_node
from core.graph import route_after_delivery, route_after_evaluator
from core.state import AgentState, LeadContext

pytestmark = pytest.mark.unit


# ── Helpers ───────────────────────────────────────────────────────────────────

def _doc(distance: float) -> MagicMock:
    """Minimal langchain Document mock with metadata.distance."""
    doc = MagicMock()
    doc.metadata = {"distance": distance}
    return doc


def _lead(tenant_id: str = "acme") -> LeadContext:
    return LeadContext(
        lead_id="lead-001",
        tenant_id=tenant_id,
        raw_payload={"text": "Testo lead."},
    )


def _base_state(**overrides) -> AgentState:
    base: AgentState = {
        "lead": _lead(),
        "messages": [],
        "retrieved_docs": [],
        "confidence_score": 0.0,
        "human_approved": None,
        "review_feedback": None,
        "status": "processing",
        "error_detail": None,
        "sanitized_text": "Testo sanitizzato.",
        "extracted_services": [],
        "mapped_services": [],
        "total_quote": 0.0,
        "on_request_services": [],
        "retry_count": 0,
        "delivery_status": "PENDING",
        "delivery_attempts": 0,
        "delivery_error": None,
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


# ═════════════════════════════════════════════════════════════════════════════
# 1. messages — operator.add (append, non replace)
# ═════════════════════════════════════════════════════════════════════════════

class TestMessagesReducer:
    """
    AgentState.messages usa Annotated[List[BaseMessage], operator.add].
    Questo significa che LangGraph applica operator.add (concatenazione di liste)
    al merge dei risultati dei nodi, NON una semplice sostituzione.

    Questo è critico per la semantica fan-in: se due nodi scrivono messages
    in parallelo, entrambi i risultati vengono preservati, non il solo ultimo.
    """

    def test_operator_add_appends_not_replaces(self) -> None:
        """operator.add su liste è concatenazione, non replacement."""
        existing = ["msg-a", "msg-b"]
        new = ["msg-c"]
        result = operator.add(existing, new)
        assert result == ["msg-a", "msg-b", "msg-c"]

    def test_operator_add_preserves_existing_messages(self) -> None:
        """Aggiungere messaggi non cancella quelli precedenti."""
        history = ["turn-1", "turn-2"]
        addition = ["turn-3"]
        merged = operator.add(history, addition)
        assert "turn-1" in merged
        assert "turn-3" in merged

    def test_operator_add_with_empty_new_list_is_idempotent(self) -> None:
        """Se un nodo non aggiunge messaggi, la storia rimane invariata."""
        history = ["turn-1"]
        assert operator.add(history, []) == history

    def test_operator_add_with_empty_existing_list(self) -> None:
        """Primo messaggio in un thread vuoto."""
        result = operator.add([], ["first-msg"])
        assert result == ["first-msg"]

    def test_two_fan_in_nodes_both_messages_preserved(self) -> None:
        """
        Simula due nodi che scrivono messages in parallelo (fan-in).
        Con operator.add il merge è commutativo: entrambi i messaggi vengono
        preservati indipendentemente dall'ordine di esecuzione.
        """
        existing: list[str] = []
        node_a_output = ["msg-from-a"]
        node_b_output = ["msg-from-b"]
        # LangGraph applica operator.add sequenzialmente
        after_a = operator.add(existing, node_a_output)
        after_ab = operator.add(after_a, node_b_output)
        assert "msg-from-a" in after_ab
        assert "msg-from-b" in after_ab


# ═════════════════════════════════════════════════════════════════════════════
# 2. LeadContext immutability — nessun nodo deve mutare il contesto di input
# ═════════════════════════════════════════════════════════════════════════════

class TestLeadContextImmutability:
    """
    LeadContext è il contratto iniziale scritto dall'API layer e letto da tutti
    i nodi a valle. Se un nodo lo muta, il tenant_id o lead_id potrebbe essere
    diverso da quello autenticato dal JWT — un bug silenzioso e potenzialmente
    un vettore di privilege escalation.
    """

    def test_lead_context_is_pydantic_basemodel(self) -> None:
        """LeadContext deve essere un Pydantic BaseModel per avere validazione runtime."""
        from pydantic import BaseModel
        assert issubclass(LeadContext, BaseModel)

    def test_lead_context_fields_are_accessible(self) -> None:
        lead = _lead()
        assert lead.lead_id == "lead-001"
        assert lead.tenant_id == "acme"
        assert lead.raw_payload["text"] == "Testo lead."

    def test_lead_context_tenant_id_unchanged_after_sanitizer(self, make_lead_state) -> None:
        """sanitizer_node NON deve modificare lead.tenant_id."""
        from agents.sanitizer import sanitizer_node

        state = make_lead_state(raw_text="Test senza PII.")
        original_tenant = state["lead"].tenant_id
        result = sanitizer_node(state)
        # Lo stato originale non viene modificato (LangGraph fa merge, non mutazione)
        assert state["lead"].tenant_id == original_tenant
        # sanitizer_node non restituisce mai "lead" nel suo output dict
        assert "lead" not in result

    def test_sanitizer_node_does_not_return_lead_key(self, make_lead_state) -> None:
        """Nessun nodo deve restituire 'lead' nel proprio output dict."""
        from agents.sanitizer import sanitizer_node

        state = make_lead_state(raw_text="Testo pulito.")
        result = sanitizer_node(state)
        assert "lead" not in result, (
            "sanitizer_node ha restituito 'lead' nel proprio dict. "
            "Un nodo non deve mai sovrascrivere LeadContext."
        )

    def test_calculator_node_does_not_return_lead_key(self, make_lead_state) -> None:
        """calculator_node non deve sovrascrivere il contesto lead."""
        from agents.calculator import calculator_node

        state = make_lead_state(
            mapped_services=[{"matched_name": "SEO", "price": 200.0, "price_type": "FIXED"}]
        )
        result = calculator_node(state)
        assert "lead" not in result


# ═════════════════════════════════════════════════════════════════════════════
# 3. EvaluatorNode — hard-zero conditions e clamping
# ═════════════════════════════════════════════════════════════════════════════

class TestEvaluatorStateInvariants:
    """
    L'evaluator è il gatekeeper della qualità: un bug qui fa sì che preventivi
    inaffidabili arrivino al cliente senza revisione umana.
    """

    @pytest.mark.asyncio
    async def test_hard_zero_when_no_extracted_services(self) -> None:
        """Nessun servizio estratto → score=0.0, nessuna eccezione."""
        state = _base_state(
            extracted_services=[],
            mapped_services=[{"service": "SEO", "price": 200.0}],
            retrieved_docs=[_doc(0.1)],
        )
        result = await evaluator_node(state)
        assert result["confidence_score"] == 0.0

    @pytest.mark.asyncio
    async def test_hard_zero_when_no_mapped_services(self) -> None:
        """Mapper vuoto → score=0.0 (catalogo non trovato)."""
        state = _base_state(
            extracted_services=["Cloud Migration"],
            mapped_services=[],
            retrieved_docs=[_doc(0.1)],
        )
        result = await evaluator_node(state)
        assert result["confidence_score"] == 0.0

    @pytest.mark.asyncio
    async def test_hard_zero_when_no_retrieved_docs(self) -> None:
        """
        Nessun documento recuperato dal vector store → score=0.0.
        V2.1 fix: il vecchio fallback su mapped_ratio era un falso positivo
        quando il catalogo era vuoto — un preventivo veniva approvato senza
        evidenza dal catalogo.
        """
        state = _base_state(
            extracted_services=["Cloud Migration"],
            mapped_services=[{"service": "Cloud", "price": 1000.0}],
            retrieved_docs=[],  # Vector store ha restituito zero documenti
        )
        result = await evaluator_node(state)
        assert result["confidence_score"] == 0.0

    @pytest.mark.asyncio
    async def test_score_clamped_to_one_when_many_mapped(self) -> None:
        """
        mapped_ratio cap fix: k=3 nearest-neighbour per extracted=1 → raw ratio=3.0.
        Senza il cap, score supererebbe 1.0 e il HITL threshold verrebbe
        falsamente bypassato.
        """
        state = _base_state(
            extracted_services=["Cloud"],  # 1 servizio richiesto
            mapped_services=[  # 3 corrispondenze trovate (k=3 pgvector)
                {"service": "Cloud A", "price": 100.0},
                {"service": "Cloud B", "price": 200.0},
                {"service": "Cloud C", "price": 300.0},
            ],
            retrieved_docs=[_doc(0.05), _doc(0.08), _doc(0.06)],
        )
        result = await evaluator_node(state)
        assert result["confidence_score"] <= 1.0, (
            "confidence_score supera 1.0: il cap a mapped_ratio non funziona. "
            "Questo bypasserebbe silenziosamente il HITL threshold."
        )

    @pytest.mark.asyncio
    async def test_score_never_below_zero(self) -> None:
        """Il clamping inferiore garantisce score >= 0.0."""
        state = _base_state(
            extracted_services=["X"],
            mapped_services=[{"service": "X", "price": 0.0}],
            retrieved_docs=[_doc(1.5)],  # Distanza coseno > 1.0 (anomalia)
        )
        result = await evaluator_node(state)
        assert result["confidence_score"] >= 0.0

    @pytest.mark.asyncio
    async def test_high_confidence_with_perfect_match(self) -> None:
        """Distanza 0.0 e 1 servizio estratto/mappato → score = 1.0."""
        state = _base_state(
            extracted_services=["SEO Audit"],
            mapped_services=[{"service": "SEO Audit", "price": 500.0}],
            retrieved_docs=[_doc(0.0)],
        )
        result = await evaluator_node(state)
        assert result["confidence_score"] == 1.0

    @pytest.mark.asyncio
    async def test_evaluator_does_not_overwrite_lead_or_messages(self) -> None:
        """L'evaluator deve scrivere solo confidence_score (e opzionalmente status)."""
        state = _base_state(
            extracted_services=["X"],
            mapped_services=[],
            retrieved_docs=[],
        )
        result = await evaluator_node(state)
        # Non deve sovrascrivere campi di altri nodi
        assert "lead" not in result
        assert "messages" not in result
        assert "sanitized_text" not in result
        assert "mapped_services" not in result

    @pytest.mark.asyncio
    async def test_evaluator_sets_pending_review_when_retries_exhausted(self) -> None:
        """Quando i retry sono esauriti e score < threshold → status=pending_review."""
        from core.config import get_settings
        settings = get_settings()
        state = _base_state(
            extracted_services=["X"],
            mapped_services=[],
            retrieved_docs=[],
            retry_count=settings.max_retry_count,  # Retry esauriti
        )
        result = await evaluator_node(state)
        assert result.get("status") == "pending_review"

    @pytest.mark.asyncio
    async def test_evaluator_does_not_set_status_when_retries_available(self) -> None:
        """Con retry ancora disponibili, l'evaluator NON deve impostare pending_review."""
        state = _base_state(
            extracted_services=["X"],
            mapped_services=[],
            retrieved_docs=[],
            retry_count=0,  # Primo tentativo, non si va in HITL
        )
        result = await evaluator_node(state)
        # status non deve cambiare in pending_review se c'è ancora un retry
        assert result.get("status") != "pending_review"


# ═════════════════════════════════════════════════════════════════════════════
# 4. route_after_evaluator — tutte le branch del router
# ═════════════════════════════════════════════════════════════════════════════

class TestRouteAfterEvaluator:
    """
    route_after_evaluator è la funzione di routing che determina se il lead
    procede verso il calcolo, viene ritentato o va in revisione umana.
    Un bug qui causa loop infiniti o bypass del HITL.
    """

    def test_routes_to_calculator_when_score_above_threshold(self, make_lead_state) -> None:
        state = make_lead_state(confidence_score=0.90, retry_count=0)
        assert route_after_evaluator(state) == "calculator"

    def test_routes_to_calculator_at_exact_threshold(self, make_lead_state) -> None:
        """Il threshold è inclusivo: score == threshold → calculator."""
        from core.config import get_settings
        threshold = get_settings().evaluator_threshold
        state = make_lead_state(confidence_score=threshold, retry_count=0)
        assert route_after_evaluator(state) == "calculator"

    def test_routes_to_extractor_when_score_below_threshold_and_retries_available(
        self, make_lead_state
    ) -> None:
        from core.config import get_settings
        settings = get_settings()
        state = make_lead_state(
            confidence_score=settings.evaluator_threshold - 0.01,
            retry_count=0,  # Ancora sotto max_retry_count
        )
        assert route_after_evaluator(state) == "extractor"

    def test_routes_to_hitl_when_retries_exhausted(self, make_lead_state) -> None:
        """Retries esauriti e score basso → hitl_interrupt (non loop infinito)."""
        from core.config import get_settings
        settings = get_settings()
        state = make_lead_state(
            confidence_score=0.0,
            retry_count=settings.max_retry_count,  # Esauriti
        )
        assert route_after_evaluator(state) == "hitl_interrupt"

    def test_never_routes_to_hitl_when_retries_available(self, make_lead_state) -> None:
        """Con retry disponibili, il router non deve andare in HITL."""
        from core.config import get_settings
        settings = get_settings()
        state = make_lead_state(confidence_score=0.0, retry_count=0)
        # Assumendo max_retry_count > 0 (default=2)
        assert settings.max_retry_count > 0
        result = route_after_evaluator(state)
        assert result != "hitl_interrupt"

    def test_exactly_at_max_retry_routes_to_hitl(self, make_lead_state) -> None:
        """
        retry_count == max_retry_count è il confine critico: HITL attivato.
        Off-by-one qui causa un loop infinito in produzione.
        """
        from core.config import get_settings
        settings = get_settings()
        state = make_lead_state(
            confidence_score=0.0,
            retry_count=settings.max_retry_count,
        )
        assert route_after_evaluator(state) == "hitl_interrupt"

    def test_one_below_max_retry_routes_to_extractor(self, make_lead_state) -> None:
        """retry_count == max_retry_count - 1 → ancora un retry, non HITL."""
        from core.config import get_settings
        settings = get_settings()
        if settings.max_retry_count == 0:
            pytest.skip("max_retry_count=0: nessun retry configurato")
        state = make_lead_state(
            confidence_score=0.0,
            retry_count=settings.max_retry_count - 1,
        )
        assert route_after_evaluator(state) == "extractor"


# ═════════════════════════════════════════════════════════════════════════════
# 5. route_after_delivery — tutte le branch
# ═════════════════════════════════════════════════════════════════════════════

class TestRouteAfterDelivery:
    def test_success_routes_to_end(self, make_lead_state) -> None:
        state = make_lead_state(delivery_status="SUCCESS", delivery_attempts=1)
        assert route_after_delivery(state) == "__end__"

    def test_failed_with_attempts_left_retries(self, make_lead_state) -> None:
        from core.config import get_settings
        settings = get_settings()
        state = make_lead_state(
            delivery_status="FAILED",
            delivery_attempts=1,  # < delivery_max_attempts (default=3)
        )
        assert settings.delivery_max_attempts > 1
        assert route_after_delivery(state) == "delivery"

    def test_failed_attempts_exhausted_routes_to_end(self, make_lead_state) -> None:
        from core.config import get_settings
        settings = get_settings()
        state = make_lead_state(
            delivery_status="FAILED",
            delivery_attempts=settings.delivery_max_attempts,
        )
        assert route_after_delivery(state) == "__end__"


# ═════════════════════════════════════════════════════════════════════════════
# 6. State field isolation — un nodo fallito non corrompe gli altri campi
# ═════════════════════════════════════════════════════════════════════════════

class TestStateFieldIsolation:
    """
    LangGraph applica il dict di output di un nodo come merge (non sostituzione)
    sullo stato corrente. Verifica che i campi non toccati da un nodo fallito
    vengano preservati nello stato risultante.
    """

    def test_calculator_error_preserves_mapped_services(self, make_lead_state) -> None:
        """
        Se calculator_node incontra un entry FIXED malformato, deve impostare
        error_detail ma NON cancellare mapped_services (serve per il debug).
        """
        from agents.calculator import calculator_node

        services = [{"service": "X", "price_type": "FIXED"}]  # Manca 'price'
        state = make_lead_state(mapped_services=services)
        result = calculator_node(state)

        # error_detail impostato
        assert result.get("error_detail") is not None
        # mapped_services NON viene toccato dal calculator (non è nel suo output)
        assert "mapped_services" not in result

    @pytest.mark.asyncio
    async def test_evaluator_error_does_not_reset_retry_count(self) -> None:
        """
        evaluator_node non deve resettare retry_count, che appartiene
        all'extractor. Se lo resettasse, il loop potrebbe diventare infinito.
        """
        state = _base_state(
            extracted_services=[],
            mapped_services=[],
            retrieved_docs=[],
            retry_count=2,
        )
        result = await evaluator_node(state)
        # evaluator non scrive mai retry_count nel proprio output
        assert "retry_count" not in result

    def test_sanitizer_does_not_overwrite_delivery_status(self, make_lead_state) -> None:
        """sanitizer_node viene eseguito solo all'inizio, non deve toccare i campi delivery."""
        from agents.sanitizer import sanitizer_node

        state = make_lead_state(raw_text="Testo pulito.")
        state["delivery_status"] = "SUCCESS"  # Impostato artificialmente
        result = sanitizer_node(state)
        # sanitizer non deve mai scrivere delivery_status
        assert "delivery_status" not in result
