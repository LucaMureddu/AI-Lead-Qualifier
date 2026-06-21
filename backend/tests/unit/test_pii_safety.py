"""
tests/unit/test_pii_safety.py
------------------------------
Verifica che nessun dato sensibile (PII) venga loggato o inviato in chiaro
all'LLM. In ambito B2B enterprise questo è un requisito legale (GDPR/NIS2)
e costituisce un single point of failure se violato.

I tre livelli di difesa da verificare:
  1. Logging: _drop_pii_processor rimuove le chiavi PII prima della
     serializzazione JSON, safe_state_log_context non espone mai raw_payload.
  2. Prompt isolation: l'extractor costruisce il prompt usando sanitized_text
     (output del SanitizerNode), mai raw_payload["text"].
  3. Sanitizer invariant: dopo sanitizer_node nessun pattern PII noto sopravvive
     nel campo sanitized_text che verrà passato all'LLM.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents.sanitizer import sanitizer_node
from core.logging_setup import (
    SAFE_STATE_LOG_KEYS,
    _drop_pii_processor,
    safe_state_log_context,
)
from core.state import LeadContext

pytestmark = pytest.mark.unit

# ── Fixture helpers ───────────────────────────────────────────────────────────

_LEAD_WITH_PII = LeadContext(
    lead_id="lead-pii-001",
    tenant_id="acme",
    raw_payload={
        "text": (
            "Contattami a mario.rossi@esempio.it, tel +39 06 12345678. "
            "CF: RSSMRA85T10A562S. "
            "Servizi richiesti: migrazione cloud e SEO audit."
        )
    },
)

_PII_FRAGMENTS = [
    "mario.rossi@esempio.it",
    "+39 06 12345678",
    "RSSMRA85T10A562S",
]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Logging layer — _drop_pii_processor
# ═════════════════════════════════════════════════════════════════════════════

class TestDropPiiProcessor:
    """Verifica che il processor structlog elimini le chiavi PII prima del log."""

    def _run(self, event_dict: dict) -> dict:
        return _drop_pii_processor(MagicMock(), "info", dict(event_dict))

    def test_drops_raw_text_key(self) -> None:
        out = self._run({"event": "test", "raw_text": "mario@esempio.it mi ha scritto"})
        assert "raw_text" not in out

    def test_drops_raw_payload_key(self) -> None:
        out = self._run({"event": "test", "raw_payload": {"text": "dati sensibili"}})
        assert "raw_payload" not in out

    def test_drops_messages_key(self) -> None:
        out = self._run({"event": "test", "messages": [{"role": "user", "content": "PII qui"}]})
        assert "messages" not in out

    def test_drops_retrieved_docs_key(self) -> None:
        out = self._run({"event": "test", "retrieved_docs": [{"page_content": "corpus privato"}]})
        assert "retrieved_docs" not in out

    def test_drops_email_key(self) -> None:
        out = self._run({"event": "test", "email": "mario@esempio.it"})
        assert "email" not in out

    def test_drops_phone_key(self) -> None:
        out = self._run({"event": "test", "phone": "+39 06 12345678"})
        assert "phone" not in out

    def test_adds_redacted_sentinel_for_each_dropped_key(self) -> None:
        """Ogni chiave eliminata deve produrre un sentinel <key>_REDACTED=True."""
        out = self._run({"event": "test", "raw_text": "PII", "email": "PII"})
        assert out.get("raw_text_REDACTED") is True
        assert out.get("email_REDACTED") is True

    def test_preserves_safe_keys(self) -> None:
        """Le chiavi non-PII devono sopravvivere intatte."""
        out = self._run({
            "event": "sanitizer.done",
            "lead_id": "lead-001",
            "tenant_id": "acme",
            "redactions": 3,
            "sanitized_len": 42,
        })
        assert out["lead_id"] == "lead-001"
        assert out["tenant_id"] == "acme"
        assert out["redactions"] == 3
        assert out["sanitized_len"] == 42

    def test_does_not_drop_sanitized_text_key(self) -> None:
        """sanitized_text NON è una chiave PII: è già mascherata e può essere loggata."""
        out = self._run({"event": "test", "sanitized_text": "[REDACTED] ha richiesto SEO"})
        assert "sanitized_text" in out

    def test_processor_is_idempotent_on_clean_event(self) -> None:
        """Un evento senza chiavi PII non subisce modifiche."""
        original = {"event": "evaluator.scored", "confidence_score": 0.82, "lead_id": "x"}
        out = self._run(original)
        assert out["event"] == original["event"]
        assert out["confidence_score"] == original["confidence_score"]
        # Nessun sentinel inatteso aggiunto
        redacted_keys = [k for k in out if k.endswith("_REDACTED")]
        assert redacted_keys == []


# ═════════════════════════════════════════════════════════════════════════════
# 2. safe_state_log_context — nessuna chiave PII nel context di log
# ═════════════════════════════════════════════════════════════════════════════

class TestSafeStateLogContext:
    """Verifica che safe_state_log_context restituisca solo chiavi sicure."""

    def _full_state(self) -> dict[str, Any]:
        return {
            "lead": _LEAD_WITH_PII,
            "messages": [{"role": "user", "content": "Dati sensibili"}],
            "retrieved_docs": [MagicMock()],
            "raw_text": "Testo grezzo con PII",
            "sanitized_text": "Testo mascherato",
            "extracted_services": ["SEO", "Cloud"],
            "mapped_services": [{"service": "SEO Audit", "price": 500.0}],
            "confidence_score": 0.82,
            "status": "processing",
            "retry_count": 1,
            "delivery_status": "PENDING",
            "delivery_attempts": 0,
            "error_detail": None,
        }

    def test_never_exposes_raw_payload(self) -> None:
        ctx = safe_state_log_context(self._full_state(), "thread-001")
        assert "raw_payload" not in ctx

    def test_never_exposes_raw_text(self) -> None:
        ctx = safe_state_log_context(self._full_state(), "thread-001")
        assert "raw_text" not in ctx

    def test_never_exposes_messages_content(self) -> None:
        ctx = safe_state_log_context(self._full_state(), "thread-001")
        # messages può comparire solo come count, mai come lista
        assert "messages" not in ctx
        # il count è ok
        assert ctx.get("messages_count") == 1

    def test_never_exposes_retrieved_docs_content(self) -> None:
        ctx = safe_state_log_context(self._full_state(), "thread-001")
        assert "retrieved_docs" not in ctx
        assert ctx.get("retrieved_docs_count") == 1

    def test_exposes_safe_scalar_fields(self) -> None:
        ctx = safe_state_log_context(self._full_state(), "thread-001")
        assert ctx["tenant_id"] == "acme"
        assert ctx["lead_id"] == "lead-pii-001"
        assert ctx["confidence_score"] == 0.82
        assert ctx["status"] == "processing"
        assert ctx["retry_count"] == 1
        assert ctx["thread_id"] == "thread-001"

    def test_all_returned_keys_are_in_safe_set(self) -> None:
        """
        Ogni chiave restituita deve appartenere a SAFE_STATE_LOG_KEYS o essere
        un count derivato (messages_count, retrieved_docs_count, ecc.).
        """
        ctx = safe_state_log_context(self._full_state(), "thread-001")
        allowed = set(SAFE_STATE_LOG_KEYS) | {
            f"{k}_count"
            for k in ("messages", "retrieved_docs", "extracted_services", "mapped_services")
        }
        for key in ctx:
            assert key in allowed, (
                f"Chiave non autorizzata nel log context: '{key}'. "
                "Aggiungere solo chiavi non-PII a SAFE_STATE_LOG_KEYS."
            )

    def test_returns_empty_dict_for_empty_state(self) -> None:
        ctx = safe_state_log_context({})
        # Non deve sollevare eccezioni e restituisce dizionario vuoto o con solo thread_id
        assert isinstance(ctx, dict)

    def test_json_serialisable(self) -> None:
        """Il context di log deve essere serializzabile in JSON (per structlog JSONRenderer)."""
        ctx = safe_state_log_context(self._full_state(), "thread-001")
        serialized = json.dumps(ctx)  # non deve sollevare TypeError
        assert isinstance(serialized, str)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Prompt isolation — il testo che arriva all'LLM è sanitized_text
# ═════════════════════════════════════════════════════════════════════════════

class TestPromptIsolation:
    """
    Verifica che l'ExtractorNode costruisca il prompt con sanitized_text,
    mai con raw_payload["text"] o altri campi non mascherati.
    """

    @pytest.mark.asyncio
    async def test_extractor_sends_sanitized_text_to_llm(self, make_lead_state) -> None:
        """
        Il prompt_user inviato all'LLM deve contenere sanitized_text e non il
        testo grezzo di raw_payload.

        Costruiamo uno stato in cui sanitized_text ≠ raw_payload["text"] e
        verifichiamo che il mock LLM riceva esattamente sanitized_text.
        """
        from agents.extractor import extractor_node

        raw_text = "Contattami a mario@esempio.it. Voglio un sito web."
        sanitized = "[REDACTED]. Voglio un sito web."

        state = make_lead_state(raw_text=raw_text)
        state["sanitized_text"] = sanitized
        state["retry_count"] = 0

        captured_prompts: list[dict] = []

        async def _mock_llm(prompt_system: str, prompt_user: str) -> str:
            captured_prompts.append({"system": prompt_system, "user": prompt_user})
            return '["Sito web"]'

        with patch("agents.extractor._call_openai_compatible", new=_mock_llm):
            await extractor_node(state)

        assert len(captured_prompts) == 1
        user_prompt = captured_prompts[0]["user"]

        # Il prompt deve contenere il testo sanitizzato
        assert sanitized in user_prompt

        # Il prompt NON deve contenere la email originale
        assert "mario@esempio.it" not in user_prompt

    @pytest.mark.asyncio
    async def test_extractor_does_not_use_raw_payload_text(self, make_lead_state) -> None:
        """
        Anche se raw_payload["text"] contiene PII, il prompt LLM non deve
        mai includerlo — deve usare solo sanitized_text.
        """
        from agents.extractor import extractor_node

        state = make_lead_state(raw_text="CF: RSSMRA85T10A562S. Cloud migration.")
        state["sanitized_text"] = "[REDACTED]. Cloud migration."
        state["retry_count"] = 0

        captured: list[str] = []

        async def _mock_llm(system: str, user: str) -> str:
            captured.append(user)
            return '["Cloud Migration"]'

        with patch("agents.extractor._call_openai_compatible", new=_mock_llm):
            await extractor_node(state)

        assert captured, "L'LLM non è stato chiamato"
        full_prompt = captured[0]
        assert "RSSMRA85T10A562S" not in full_prompt, (
            "Il codice fiscale originale non deve comparire nel prompt LLM."
        )

    @pytest.mark.asyncio
    async def test_extractor_short_circuits_on_empty_sanitized_text(self, make_lead_state) -> None:
        """
        Se sanitized_text è vuoto (sanitizer fallito), l'extractor deve
        fermarsi senza chiamare l'LLM.
        """
        from agents.extractor import extractor_node

        state = make_lead_state(raw_text="testo con email@pii.it")
        state["sanitized_text"] = ""  # Sanitizer non ha prodotto output

        llm_called = False

        async def _mock_llm(system: str, user: str) -> str:
            nonlocal llm_called
            llm_called = True
            return "[]"

        with patch("agents.extractor._call_openai_compatible", new=_mock_llm):
            result = await extractor_node(state)

        assert not llm_called, (
            "L'LLM non deve essere chiamato se sanitized_text è vuoto. "
            "Mandare testo grezzo all'LLM sarebbe una fuga di PII."
        )
        assert result["extracted_services"] == []
        assert result.get("error_detail") is not None


# ═════════════════════════════════════════════════════════════════════════════
# 4. Sanitizer invariant — nessun PII sopravvive in sanitized_text
# ═════════════════════════════════════════════════════════════════════════════

class TestSanitizerPiiInvariant:
    """
    Verifica che dopo sanitizer_node il campo sanitized_text non contenga
    alcun frammento PII noto. Questo è il prerequisito di sicurezza per
    tutto il resto della pipeline.
    """

    def test_sanitizer_node_removes_all_pii_from_output(self, make_lead_state) -> None:
        raw = (
            "Buongiorno, sono Mario Rossi. "
            "Email: mario.rossi@esempio.it. "
            "Tel: +39 06 12345678. "
            "CF: RSSMRA85T10A562S. "
            "IBAN: IT60X0542811101000000123456. "
            "Carta di credito: 4111 1111 1111 1111. "
            "Voglio una consulenza cloud e SEO audit."
        )
        state = make_lead_state(raw_text=raw)
        result = sanitizer_node(state)
        sanitized = result["sanitized_text"]

        pii_patterns = [
            "mario.rossi@esempio.it",
            "12345678",
            "RSSMRA85T10A562S",
            "IT60X0542811101000000123456",
            "4111",
        ]
        for pii in pii_patterns:
            assert pii not in sanitized, (
                f"PII '{pii}' trovato in sanitized_text: questo verrebbe inviato all'LLM."
            )

    def test_sanitizer_node_preserves_non_pii_content(self, make_lead_state) -> None:
        """Il contenuto business non-PII deve sopravvivere alla sanitizzazione."""
        raw = (
            "Contattami a test@esempio.com. "
            "Voglio: migrazione cloud, SEO audit, sviluppo sito web."
        )
        state = make_lead_state(raw_text=raw)
        result = sanitizer_node(state)
        sanitized = result["sanitized_text"]

        # Il contenuto business deve rimanere
        assert "migrazione cloud" in sanitized
        assert "SEO audit" in sanitized
        assert "sviluppo sito web" in sanitized

    def test_sanitizer_node_output_never_equals_raw_input_when_pii_present(
        self, make_lead_state
    ) -> None:
        """Se il testo grezzo contiene PII, sanitized_text deve essere diverso."""
        raw = "Chiamami al +39 06 12345678 per un preventivo."
        state = make_lead_state(raw_text=raw)
        result = sanitizer_node(state)
        # Il risultato deve essere diverso dall'input (la sanitizzazione ha agito)
        assert result["sanitized_text"] != raw

    def test_sanitizer_returns_error_state_on_exception(self, make_lead_state) -> None:
        """Se _mask_pii solleva un'eccezione, lo stato di errore NON deve contenere PII."""
        state = make_lead_state(raw_text="email@pii.it richiede cloud.")
        with patch("agents.sanitizer._mask_pii", side_effect=RuntimeError("boom")):
            result = sanitizer_node(state)

        assert result["sanitized_text"] == ""
        assert result.get("status") == "error"
        # Non deve trapelare raw_text nell'error_detail
        assert "email@pii.it" not in (result.get("error_detail") or "")
