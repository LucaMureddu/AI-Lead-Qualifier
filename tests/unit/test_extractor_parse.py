"""
tests/unit/test_extractor_parse.py
----------------------------------
Blindatura dei rami di ``_parse_services`` (parsing robusto dell'output LLM):
JSON pulito, fenced, estrazione di sottostringa da testo rumoroso, JSON non
valido nel fallback, JSON valido ma non-lista, assenza di parentesi.
Pure unit: nessun mock, nessuna I/O.
"""

from __future__ import annotations

import pytest

from agents.extractor import _parse_services

pytestmark = pytest.mark.unit


def test_clean_array() -> None:
    assert _parse_services('["A", "B"]') == ["A", "B"]


def test_fenced_array() -> None:
    assert _parse_services('```json\n["A"]\n```') == ["A"]


def test_embedded_array_in_noise() -> None:
    # json.loads fallisce sull'intero testo → il fallback estrae la sottostringa [..]
    assert _parse_services('Ecco i servizi: ["Web", "SEO"] — fine') == ["Web", "SEO"]


def test_embedded_but_invalid_array_returns_empty() -> None:
    # '[' e ']' presenti ma JSON non valido → anche il fallback fallisce → []
    assert _parse_services("[questo, non, valido]") == []


def test_non_list_json_returns_empty() -> None:
    # JSON valido ma non è una lista → []
    assert _parse_services('{"x": 1}') == []


def test_no_brackets_returns_empty() -> None:
    assert _parse_services("nessun JSON qui") == []
