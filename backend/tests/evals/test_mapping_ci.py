"""
tests/evals/test_mapping_ci.py
------------------------------
BINARIO A — eval model-free per il Mapper (marker ``eval_ci``, gira in CI).
Nessun ChromaDB: unisce le ATTESE (``datasets/mappings_golden.jsonl``) con le
GENERAZIONI registrate (``snapshots/mappings.json``) e verifica:

- match positivo: il miglior match contiene la categoria attesa E la distanza è
  sotto ``max_distance`` (guardrail di regressione — vedi §4.3.1, soglia stretta);
- off-target: una query fuori catalogo DEVE avere distanza ALTA (> ``min_distance``),
  cioè cadere fuori dalla soglia di accettazione.
"""

from __future__ import annotations

import json
import pathlib

import pytest

pytestmark = pytest.mark.eval_ci

_HERE = pathlib.Path(__file__).resolve().parent
_GOLDEN = [
    json.loads(line)
    for line in (_HERE / "datasets" / "mappings_golden.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
_SNAP = {
    r["query"]: r
    for r in json.loads((_HERE / "snapshots" / "mappings.json").read_text(encoding="utf-8")).get("mappings", [])
}


@pytest.mark.parametrize("row", _GOLDEN, ids=[r["query"] for r in _GOLDEN])
def test_mapping_snapshot(row: dict) -> None:
    snap = _SNAP.get(row["query"])
    if snap is None:
        pytest.skip(f"'{row['query']}' non è nello snapshot — rigenera con capture_snapshots.py")

    dist = snap.get("distance")
    assert dist is not None, f"{row['query']}: distanza mancante nello snapshot"

    # ── Off-target: deve cadere FUORI dalla soglia di accettazione ────────────
    if row.get("off_target"):
        assert dist > row["min_distance"], (
            f"{row['query']}: distanza {dist:.3f} troppo BASSA per un off-target "
            f"(soglia {row['min_distance']}) → rischio match allucinato"
        )
        return

    # ── Match positivo: categoria giusta + distanza sotto soglia ──────────────
    matched = snap.get("matched_name", "")
    assert row["expect_match_contains"].lower() in matched.lower(), (
        f"{row['query']}: match '{matched}' non contiene '{row['expect_match_contains']}'"
    )
    assert 0.0 <= dist <= row["max_distance"], (
        f"{row['query']}: distanza {dist:.3f} oltre la soglia {row['max_distance']}"
    )
