"""
tests/unit/test_sanitizer.py
----------------------------
Unit test puri per il mascheramento PII (``_mask_pii``) e ``sanitizer_node``.
Nessuna I/O, nessun mock: sono funzioni deterministiche.

Le asserzioni sono volutamente robuste (substring assente + soglia sul conteggio)
perché i pattern PII sono applicati in cascata e più pattern possono coprire la
stessa stringa: ciò che conta è che il dato sensibile sparisca.
"""

from __future__ import annotations

import pytest

from agents.sanitizer import _mask_pii, sanitizer_node

pytestmark = pytest.mark.unit


class TestMaskPii:
    def test_masks_email(self) -> None:
        text, count = _mask_pii("Contattami a john.doe@example.com grazie.", "[R]")
        assert "john.doe@example.com" not in text
        assert "[R]" in text
        assert count >= 1

    def test_masks_italian_fiscal_code(self) -> None:
        text, count = _mask_pii("CF: RSSMRA85T10A562S", "[R]")
        assert "RSSMRA85T10A562S" not in text
        assert count >= 1

    def test_masks_credit_card(self) -> None:
        text, count = _mask_pii("Carta 4111 1111 1111 1111 in scadenza", "[R]")
        assert "4111" not in text
        assert count >= 1

    def test_masks_iban(self) -> None:
        iban = "IT60X0542811101000000123456"
        text, count = _mask_pii(f"Bonifico su {iban}, grazie", "[R]")
        assert iban not in text
        assert count >= 1

    def test_masks_phone(self) -> None:
        text, count = _mask_pii("Chiamami al +39 06 12345678 domani", "[R]")
        assert "12345678" not in text
        assert count >= 1

    def test_masks_ssn(self) -> None:
        text, count = _mask_pii("SSN 123-45-6789 confidenziale", "[R]")
        assert "123-45-6789" not in text
        assert count >= 1

    def test_no_pii_unchanged(self) -> None:
        original = "We need cloud migration and SEO audit services."
        text, count = _mask_pii(original, "[R]")
        assert text == original
        assert count == 0

    def test_multiple_pii_types(self) -> None:
        masked, count = _mask_pii("Email: a@b.com, tel: +39 06 12345678", "[R]")
        assert "a@b.com" not in masked
        assert count >= 2

    def test_custom_mask_token(self) -> None:
        text, _ = _mask_pii("scrivi a a@b.com", "***")
        assert "***" in text
        assert "a@b.com" not in text


class TestSanitizerNode:
    def test_produces_sanitized_text(self, make_lead_state) -> None:
        state = make_lead_state(raw_text="Scrivimi a a@b.com per un preventivo.")
        result = sanitizer_node(state)
        assert "sanitized_text" in result
        assert "a@b.com" not in result["sanitized_text"]

    def test_error_detail_none_on_success(self, make_lead_state) -> None:
        result = sanitizer_node(make_lead_state(raw_text="Testo semplice."))
        assert result.get("error_detail") is None

    def test_initialises_retry_count(self, make_lead_state) -> None:
        result = sanitizer_node(make_lead_state(raw_text="Testo semplice."))
        assert result["retry_count"] == 0
