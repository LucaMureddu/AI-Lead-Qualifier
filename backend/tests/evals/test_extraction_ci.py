"""
tests/evals/test_extraction_ci.py
---------------------------------
BINARIO A — evals model-free, eseguiti in CI (marker ``eval_ci``, NON escluso
da ``-m "not eval"``).

Non invoca alcun LLM: legge le generazioni REGISTRATE in
``snapshots/extractions.json`` (catturate in locale col modello reale via
``capture_snapshots.py``) e le confronta con le attese del golden dataset.

Asserzioni:
- deterministiche: fallback → lista vuota; altrimenti lista non vuota e
  ``len >= min_services``;
- categoria attesa: almeno una ``expect_keywords`` compare in un servizio
  (match multilingua, robusto all'output EN/IT del modello) OPPURE similarità
  semantica ≥ soglia (embedder finto in ``semantic.py``, sostituibile con
  all-MiniLM-L6-v2).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tests.evals.semantic import semantic_score

pytestmark = pytest.mark.eval_ci

_HERE = pathlib.Path(__file__).resolve().parent
_SEMANTIC_THRESHOLD = 0.5

_GOLDEN = [
    json.loads(line)
    for line in (_HERE / "datasets" / "leads_golden.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
_SNAPSHOT = json.loads((_HERE / "snapshots" / "extractions.json").read_text(encoding="utf-8"))
_OUTPUTS = {e["id"]: e["output"] for e in _SNAPSHOT.get("extractions", [])}


def _is_fallback(row: dict) -> bool:
    return bool(row.get("expect_human_fallback")) or row.get("expect_services") == 0


def _category_matched(keywords: list[str], services: list[str]) -> bool:
    """Match deterministico (keyword) OR semantico (embedder finto)."""
    low = [s.lower() for s in services]
    if any(kw.lower() in s for s in low for kw in keywords):
        return True
    return max((semantic_score(kw, services) for kw in keywords), default=0.0) >= _SEMANTIC_THRESHOLD


@pytest.mark.parametrize("row", _GOLDEN, ids=[r["id"] for r in _GOLDEN])
def test_extraction_snapshot(row: dict) -> None:
    if row["id"] not in _OUTPUTS:
        pytest.skip(f"Nessuno snapshot per {row['id']} — rigenera con capture_snapshots.py")

    output = _OUTPUTS[row["id"]]

    # ── Fuori scope / human fallback: nessun servizio mappabile ───────────────
    if _is_fallback(row):
        assert output == [], f"{row['id']}: atteso nessun servizio (fallback), ottenuto {output}"
        return

    # ── Deterministico ────────────────────────────────────────────────────────
    assert output, f"{row['id']}: la lista dei servizi non deve essere vuota"
    assert len(output) >= row["min_services"], (
        f"{row['id']}: attesi >= {row['min_services']} servizi, ottenuti {len(output)}"
    )

    # ── Categoria attesa (keyword multilingua OR semantica) ───────────────────
    assert _category_matched(row["expect_keywords"], output), (
        f"{row['id']}: nessuna keyword {row['expect_keywords']} nei servizi {output}"
    )


def test_snapshot_covers_all_golden_ids() -> None:
    """Sanity: lo snapshot copre tutti gli id del golden (altrimenti rigenerare)."""
    missing = [r["id"] for r in _GOLDEN if r["id"] not in _OUTPUTS]
    assert not missing, f"Snapshot incompleto, mancano: {missing}"
