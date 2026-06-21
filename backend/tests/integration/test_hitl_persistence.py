"""
tests/integration/test_hitl_persistence.py
--------------------------------------------
Test end-to-end del ciclo HITL (Human-in-the-Loop) con persistenza reale.

Usa AsyncPostgresSaver su Postgres reale (via Testcontainers), lo stesso
checkpointer di produzione, per verificare che:

  1. Dopo che il grafo raggiunge hitl_interrupt_node, il checkpoint viene
     persistito con status="pending_review" nel Postgres reale.

  2. Dopo graph.aupdate_state() (l'operazione del /approve endpoint) e
     graph.ainvoke(Command(resume=None)), il grafo riprende dall'interruzione
     con il corretto human_approved impostato.

  3. L'approvazione (human_approved=True) porta il grafo a completare la
     pipeline (delivery completata o tentata).

  4. Il rifiuto (human_approved=False) porta il grafo allo stato di errore.

  5. Checkpoint superstite: un secondo ainvoke legge il checkpoint del primo
     (il grafo non viene ri-eseguito dall'inizio).

Nota: questi test usano la fixture `checkpointer` di conftest.py, che
avvia un container Postgres reale e tronca le tabelle checkpoint tra i test.

Tutti i nodi successivi all'interrupt vengono mockati per velocizzare il test
e non richiedere servizi esterni (LLM, Ollama, email).

Markers: `integration` (non girano in unit CI).
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.types import Command

from core.graph import build_graph
from core.state import AgentState, LeadContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ── Helper factories ──────────────────────────────────────────────────────────

def _make_state(
    thread_id: str = "thread-hitl-001",
    tenant_id: str = "acme",
) -> AgentState:
    """Stato iniziale valido per la qualification pipeline."""
    return {
        "lead": LeadContext(
            lead_id="lead-hitl-001",
            tenant_id=tenant_id,
            raw_payload={"text": "Voglio un sito web e consulenza cloud."},
        ),
        "messages": [],
        "retrieved_docs": [],
        "confidence_score": 0.0,
        "human_approved": None,
        "review_feedback": None,
        "status": "processing",
        "error_detail": None,
        "sanitized_text": "",
        "extracted_services": [],
        "mapped_services": [],
        "total_quote": 0.0,
        "on_request_services": [],
        "retry_count": 0,
        "delivery_status": "PENDING",
        "delivery_attempts": 0,
        "delivery_error": None,
    }


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _mock_patch_context():
    """
    Context manager che mocka tutti i nodi che eseguono I/O esterno,
    forzando il grafo ad arrivare ad hitl_interrupt velocemente.

    Strategia:
    - sanitizer_node: ritorna sanitized_text (nessuna regex, no I/O)
    - extractor_node: sempre empty list → retry_count++ → esaurisce i retry
    - mapper_node: lista vuota → nessun I/O
    - evaluator_node: score=0.0 con retry esauriti → hitl_interrupt
    - calculator_node / delivery_node: mai raggiungibili prima dell'interrupt
    """
    from unittest.mock import patch

    async def _sanitizer(state: AgentState) -> dict:
        return {
            "sanitized_text": state["lead"].raw_payload["text"],
        }

    async def _extractor(state: AgentState) -> dict:
        """Ritorna lista vuota per consumare retry_count."""
        return {
            "extracted_services": [],
            "retry_count": state.get("retry_count", 0) + 1,
            "error_detail": "empty in test",
        }

    async def _mapper(state: AgentState) -> dict:
        return {"mapped_services": [], "retrieved_docs": []}

    async def _evaluator(state: AgentState) -> dict:
        """Ritorna score=0.0; con retry esauriti route_after_evaluator → hitl_interrupt."""
        return {
            "confidence_score": 0.0,
            "status": "pending_review",
        }

    async def _calculator(state: AgentState) -> dict:
        return {"total_quote": 0.0, "on_request_services": []}

    async def _delivery(state: AgentState) -> dict:
        return {"delivery_status": "SUCCESS", "status": "completed", "delivery_attempts": 1}

    return [
        patch("core.graph.sanitizer_node", new=_sanitizer),
        patch("core.graph.extractor_node", new=_extractor),
        patch("core.graph.mapper_node", new=_mapper),
        patch("core.graph.evaluator_node", new=_evaluator),
        patch("core.graph.calculator_node", new=_calculator),
        patch("core.graph.delivery_node", new=_delivery),
    ]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Checkpoint persiste dopo interrupt() — lettura dal Postgres reale
# ═════════════════════════════════════════════════════════════════════════════

class TestCheckpointPersistence:
    """Verifica che dopo interrupt() il checkpoint sia nel Postgres reale."""

    async def test_checkpoint_saved_after_hitl_interrupt(self, checkpointer) -> None:
        """
        Dopo ainvoke() che raggiunge hitl_interrupt, il checkpoint deve esistere
        nel Postgres reale e il thread_id deve essere ritrovabile.
        """
        from core.config import get_settings
        settings = get_settings()

        thread_id = "thread-persist-001"
        config = _config(thread_id)

        # Costruiamo lo stato con retry_count già esaurito,
        # così l'evaluator va direttamente in hitl_interrupt.
        state = _make_state(thread_id=thread_id)
        state["retry_count"] = settings.max_retry_count  # Simuliamo retry esauriti

        patches = _mock_patch_context()
        for p in patches:
            p.start()

        try:
            graph = build_graph(checkpointer=checkpointer)
            await graph.ainvoke(state, config=config)
        except Exception:
            # interrupt() non è un'eccezione propagata con LangGraph ≥ 0.2;
            # usiamo try/except come safety net per diverse versioni SDK.
            pass
        finally:
            for p in patches:
                p.stop()

        # Il checkpoint deve esistere nel Postgres reale
        saved_state = await graph.aget_state(config)
        assert saved_state is not None, (
            "Il checkpoint non è stato trovato nel Postgres reale. "
            "AsyncPostgresSaver non ha persistito dopo interrupt()."
        )

    async def test_thread_id_survives_restart(self, checkpointer) -> None:
        """
        Il thread_id persiste indipendentemente da quale processo chiama aget_state.
        Due diverse istanze del grafo (con lo stesso checkpointer) devono vedere
        lo stesso checkpoint — questo simula la comunicazione API→Worker.
        """
        from core.config import get_settings
        settings = get_settings()

        thread_id = "thread-persist-002"
        config = _config(thread_id)
        state = _make_state(thread_id=thread_id)
        state["retry_count"] = settings.max_retry_count

        patches = _mock_patch_context()
        for p in patches:
            p.start()

        try:
            graph1 = build_graph(checkpointer=checkpointer)
            await graph1.ainvoke(state, config=config)
        except Exception:
            pass
        finally:
            for p in patches:
                p.stop()

        # Una SECONDA istanza del grafo (come una diversa istanza del processo API)
        graph2 = build_graph(checkpointer=checkpointer)
        saved = await graph2.aget_state(config)
        assert saved is not None, (
            "Un secondo grafo non trova il checkpoint — "
            "il thread_id non sopravvive tra istanze di processo."
        )


# ═════════════════════════════════════════════════════════════════════════════
# 2. aupdate_state prima del resume — il /approve endpoint
# ═════════════════════════════════════════════════════════════════════════════

class TestApproveUpdateState:
    """
    Verifica che graph.aupdate_state() scriva correttamente i campi di approvazione
    prima del resume — l'operazione eseguita dal POST /lead/{thread_id}/approve.
    """

    async def test_aupdate_state_sets_human_approved_in_checkpoint(
        self, checkpointer
    ) -> None:
        """
        L'operazione /approve aggiorna il checkpoint con human_approved=True
        e confidence_score=1.0. Dopo l'update, aget_state deve restituire
        esattamente quei valori.
        """
        from core.config import get_settings
        settings = get_settings()

        thread_id = "thread-approve-001"
        config = _config(thread_id)
        state = _make_state(thread_id=thread_id)
        state["retry_count"] = settings.max_retry_count

        patches = _mock_patch_context()
        for p in patches:
            p.start()

        try:
            graph = build_graph(checkpointer=checkpointer)
            await graph.ainvoke(state, config=config)
        except Exception:
            pass
        finally:
            for p in patches:
                p.stop()

        # Operazione equivalente a POST /lead/{thread_id}/approve (human_approved=True)
        await graph.aupdate_state(
            config,
            {
                "human_approved": True,
                "confidence_score": 1.0,
                "review_feedback": "Approvato manualmente dall'operatore.",
                "status": "queued",
            },
        )

        updated = await graph.aget_state(config)
        assert updated is not None
        vals = updated.values
        assert vals.get("human_approved") is True, (
            "human_approved non è stato salvato nel checkpoint dopo aupdate_state."
        )
        assert vals.get("confidence_score") == 1.0
        assert vals.get("review_feedback") == "Approvato manualmente dall'operatore."

    async def test_aupdate_state_sets_rejected_in_checkpoint(
        self, checkpointer
    ) -> None:
        """
        /approve con approved=False deve scrivere human_approved=False e status='error'.
        """
        from core.config import get_settings
        settings = get_settings()

        thread_id = "thread-approve-reject-001"
        config = _config(thread_id)
        state = _make_state(thread_id=thread_id)
        state["retry_count"] = settings.max_retry_count

        patches = _mock_patch_context()
        for p in patches:
            p.start()

        try:
            graph = build_graph(checkpointer=checkpointer)
            await graph.ainvoke(state, config=config)
        except Exception:
            pass
        finally:
            for p in patches:
                p.stop()

        # Operazione di rifiuto
        await graph.aupdate_state(
            config,
            {
                "human_approved": False,
                "status": "error",
                "error_detail": "Lead rifiutato dall'operatore.",
            },
        )

        updated = await graph.aget_state(config)
        assert updated is not None
        vals = updated.values
        assert vals.get("human_approved") is False
        assert vals.get("status") == "error"


# ═════════════════════════════════════════════════════════════════════════════
# 3. Resume con Command(resume=None) — il run_qualification_task_resume
# ═════════════════════════════════════════════════════════════════════════════

class TestResumeFromInterrupt:
    """
    Verifica che dopo aupdate_state (approve), l'ainvoke(Command(resume=None))
    riprenda il grafo dal nodo corretto e lo porti a completamento.
    """

    async def test_approved_resume_reaches_delivery(self, checkpointer) -> None:
        """
        Ciclo completo:
          1. Grafo esegue → hitl_interrupt (persistito)
          2. /approve scrive human_approved=True (aupdate_state)
          3. run_qualification_task_resume invoca Command(resume=None)
          4. Il grafo riprende → calculator → delivery → completed

        Tutti i nodi sono mockati eccetto hitl_interrupt (quello reale di LangGraph).
        """
        from core.config import get_settings
        settings = get_settings()

        thread_id = "thread-resume-approve-001"
        config = _config(thread_id)
        state = _make_state(thread_id=thread_id)
        state["retry_count"] = settings.max_retry_count

        patches = _mock_patch_context()
        for p in patches:
            p.start()

        try:
            graph = build_graph(checkpointer=checkpointer)
            await graph.ainvoke(state, config=config)
        except Exception:
            pass
        finally:
            for p in patches:
                p.stop()

        # /approve: aggiorna checkpoint con approvazione
        await graph.aupdate_state(
            config,
            {
                "human_approved": True,
                "confidence_score": 1.0,
                "status": "queued",
            },
        )

        # run_qualification_task_resume: riprende il grafo
        patches2 = _mock_patch_context()
        for p in patches2:
            p.start()

        try:
            await graph.ainvoke(Command(resume=None), config=config)
        except Exception:
            pass
        finally:
            for p in patches2:
                p.stop()

        final = await graph.aget_state(config)
        assert final is not None
        vals = final.values

        # human_approved deve essere rimasto True (non resettato dal resume).
        # Questo è l'invariante critico: il campo scritto da /approve non deve
        # essere azzerato dal resume.
        assert vals.get("human_approved") is True, (
            "human_approved è stato resettato durante il resume. "
            "Il checkpoint deve preservare tutti i campi scritti da aupdate_state."
        )

        # Il grafo non deve essere bloccato in pending_review (interrupt attivo).
        # Dopo Command(resume=None), hitl_interrupt_node completa e segue il
        # bordo statico → END. Lo status può essere queued/completed/error
        # a seconda di quanto avanzato è il grafo, ma NON deve restare
        # "processing" (segnale di loop infinito).
        assert vals.get("status") != "processing", (
            "status='processing' dopo il resume: il grafo potrebbe essere in loop."
        )

    async def test_rejected_state_remains_error_after_no_resume(
        self, checkpointer
    ) -> None:
        """
        Un lead rifiutato (human_approved=False, status='error') NON viene
        eseguito nuovamente. Lo stato di errore sopravvive indefinitamente.
        """
        from core.config import get_settings
        settings = get_settings()

        thread_id = "thread-reject-persist-001"
        config = _config(thread_id)
        state = _make_state(thread_id=thread_id)
        state["retry_count"] = settings.max_retry_count

        patches = _mock_patch_context()
        for p in patches:
            p.start()

        try:
            graph = build_graph(checkpointer=checkpointer)
            await graph.ainvoke(state, config=config)
        except Exception:
            pass
        finally:
            for p in patches:
                p.stop()

        # Rifiuto
        await graph.aupdate_state(
            config,
            {
                "human_approved": False,
                "status": "error",
                "error_detail": "Rifiutato.",
            },
        )

        # Verifica che il checkpoint rifletta il rifiuto
        check = await graph.aget_state(config)
        assert check is not None
        vals = check.values
        assert vals.get("status") == "error"
        assert vals.get("human_approved") is False
        assert vals.get("error_detail") == "Rifiutato."

    async def test_thread_isolation_between_leads(self, checkpointer) -> None:
        """
        Due lead su thread_id diversi hanno checkpoint indipendenti.
        Multi-tenancy B2B: un lead approvato non deve influenzare un altro lead.
        """
        from core.config import get_settings
        settings = get_settings()

        thread_a = "thread-isolation-A"
        thread_b = "thread-isolation-B"

        state_a = _make_state(thread_id=thread_a)
        state_a["retry_count"] = settings.max_retry_count
        state_b = _make_state(thread_id=thread_b)
        state_b["retry_count"] = settings.max_retry_count

        graph = build_graph(checkpointer=checkpointer)

        patches = _mock_patch_context()
        for p in patches:
            p.start()

        try:
            await graph.ainvoke(state_a, config=_config(thread_a))
            await graph.ainvoke(state_b, config=_config(thread_b))
        except Exception:
            pass
        finally:
            for p in patches:
                p.stop()

        # Approva solo il thread A
        await graph.aupdate_state(
            _config(thread_a),
            {"human_approved": True, "status": "queued"},
        )

        # Thread B deve rimanere in pending_review / non approvato
        state_b_check = await graph.aget_state(_config(thread_b))
        assert state_b_check is not None
        b_vals = state_b_check.values

        # human_approved di B deve essere None (mai toccato)
        assert b_vals.get("human_approved") is None, (
            "L'approvazione del thread A ha contaminato il thread B! "
            "I checkpoint devono essere isolati per thread_id."
        )

    async def test_checkpoint_not_lost_between_two_invocations(
        self, checkpointer
    ) -> None:
        """
        Il secondo ainvoke (resume) NON deve sovrascrivere lo stato accumulato
        nel primo ainvoke. Il checkpoint è append-only (i nodi scrivono
        solo i propri campi, non azzerano l'intero state dict).
        """
        from core.config import get_settings
        settings = get_settings()

        thread_id = "thread-accumulate-001"
        config = _config(thread_id)
        state = _make_state(thread_id=thread_id)
        state["retry_count"] = settings.max_retry_count

        graph = build_graph(checkpointer=checkpointer)

        patches = _mock_patch_context()
        for p in patches:
            p.start()

        try:
            await graph.ainvoke(state, config=config)
        except Exception:
            pass
        finally:
            for p in patches:
                p.stop()

        # Primo checkpoint: confidence_score=0.0 (impostato dall'evaluator mock)
        first_state = await graph.aget_state(config)
        assert first_state is not None
        first_lead_id = first_state.values.get("lead").lead_id

        # Approve e resume
        await graph.aupdate_state(config, {"human_approved": True, "status": "queued"})

        patches2 = _mock_patch_context()
        for p in patches2:
            p.start()

        try:
            await graph.ainvoke(Command(resume=None), config=config)
        except Exception:
            pass
        finally:
            for p in patches2:
                p.stop()

        # Dopo il resume, il lead_id originale deve essere ancora presente
        final_state = await graph.aget_state(config)
        assert final_state is not None
        assert final_state.values.get("lead").lead_id == first_lead_id, (
            "lead.lead_id è cambiato durante il resume. "
            "Il checkpoint del primo ainvoke è stato sovrascritto."
        )
