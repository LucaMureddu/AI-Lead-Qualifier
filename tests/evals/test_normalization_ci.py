"""
tests/evals/test_normalization_ci.py
------------------------------------
BINARIO A — eval model-free per Normalizer + Routing HITL (marker ``eval_ci``,
gira in CI). Nessun LLM: legge ``snapshots/normalizations.json`` (catturato in
locale col modello reale) e verifica le proprietà attese.

Capacità coperte (TESTING_PLAN.md §4.1):
- Normalizer (§4.1.3): il catalogo "sporco" → schema canonico. Invarianti
  deterministiche (§4.3.1): currency == "EUR", price >= 0, prezzi reali estratti.
- Routing HITL (§4.1.4): il catalogo "problematico" DEVE produrre flag
  (``flagged_count > 0``) — è l'eval gratuito e particolarmente importante.
"""

from __future__ import annotations

import json
import pathlib

import pytest

pytestmark = pytest.mark.eval_ci

_HERE = pathlib.Path(__file__).resolve().parent
_SNAP = json.loads((_HERE / "snapshots" / "normalizations.json").read_text(encoding="utf-8"))
_CATALOGS = _SNAP.get("catalogs", {})


@pytest.mark.parametrize("catalog", list(_CATALOGS.keys()) or ["(snapshot-vuoto)"])
def test_normalization_invariants(catalog: str) -> None:
    if catalog not in _CATALOGS:
        pytest.skip("Snapshot vuoto — rigenera con capture_snapshots.py")
    items = _CATALOGS[catalog]["items"]
    assert items, f"{catalog}: nessun item normalizzato"
    for it in items:
        assert it["currency"] == "EUR", f"{catalog}: currency != EUR → {it}"
        assert it["price"] >= 0, f"{catalog}: prezzo negativo → {it}"


def test_dirty_catalog_extracts_real_prices() -> None:
    data = _CATALOGS.get("dirty_catalog.csv")
    if not data:
        pytest.skip("Snapshot vuoto — rigenera con capture_snapshots.py")
    # Almeno una riga valida normalizzata correttamente (prezzo > 0, non flaggata):
    # prova che la normalizzazione non ha semplicemente flaggato tutto.
    assert any(it["price"] > 0 and not it["flagged"] for it in data["items"]), (
        "Nessun prezzo valido estratto dal catalogo sporco"
    )


def test_problematic_catalog_must_flag() -> None:
    data = _CATALOGS.get("catalogo_problematico.csv")
    if not data:
        pytest.skip("Snapshot vuoto — rigenera con capture_snapshots.py")
    # HITL: il sistema deve "dubitare" su prezzi 0/negativi/"gratis" e nomi di 1 carattere.
    assert data["flagged_count"] > 0, "Il catalogo problematico DEVE produrre flag (HITL)"
