"""
tests/evals/test_normalization_eval.py
---------------------------------------
BINARIO B — eval LIVE per Normalizer + Routing HITL (marker ``eval``, escluso da
``-m "not eval"``). Esegue la pipeline REALE (chunker → normalizer LLM →
validator) sui CSV esistenti, con Ollama attivo:

    pytest -m eval

- ``dirty_catalog.csv``        → invarianti canoniche (currency EUR, price >= 0).
- ``catalogo_problematico.csv``→ deve flaggare (routing HITL).
"""

from __future__ import annotations

import pathlib

import pytest

from tests.evals._pipeline import run_normalization

pytestmark = pytest.mark.eval

_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("catalog", ["dirty_catalog.csv", "catalogo_problematico.csv"])
async def test_normalization_invariants_live(catalog: str) -> None:
    res = await run_normalization(_ROOT / catalog)
    assert not res.get("error"), f"{catalog}: errore pipeline → {res.get('error')}"
    assert res["items"], f"{catalog}: nessun item normalizzato"
    for it in res["items"]:
        assert it["currency"] == "EUR", f"{catalog}: currency != EUR → {it}"
        assert it["price"] >= 0, f"{catalog}: prezzo negativo → {it}"


async def test_dirty_catalog_extracts_real_prices_live() -> None:
    res = await run_normalization(_ROOT / "dirty_catalog.csv")
    assert any(it["price"] > 0 and not it["flagged"] for it in res["items"]), (
        "Nessun prezzo valido estratto dal catalogo sporco"
    )


async def test_problematic_catalog_must_flag_live() -> None:
    res = await run_normalization(_ROOT / "catalogo_problematico.csv")
    assert res["flagged_count"] > 0, "Il catalogo problematico DEVE produrre flag (HITL)"
