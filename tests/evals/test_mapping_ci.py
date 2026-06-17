"""
tests/evals/test_mapping_ci.py
------------------------------
BINARIO A — eval model-free per il Mapper (marker ``eval_ci``, gira in CI).
Nessun ChromaDB: legge ``snapshots/mappings.json`` (catturato in locale) e
verifica che, per ogni query, il miglior match sia quello atteso e la distanza
sia sotto la soglia.
"""

from __future__ import annotations

import json
import pathlib

import pytest

pytestmark = pytest.mark.eval_ci

_HERE = pathlib.Path(__file__).resolve().parent
_ROWS = json.loads((_HERE / "snapshots" / "mappings.json").read_text(encoding="utf-8")).get("mappings", [])


@pytest.mark.parametrize("row", _ROWS or [None], ids=[r["query"] for r in _ROWS] or ["(snapshot-vuoto)"])
def test_mapping_snapshot(row) -> None:
    if row is None:
        pytest.skip("Snapshot vuoto — rigenera con capture_snapshots.py (richiede Chroma)")

    matched = row.get("matched_name", "")
    assert matched, f"{row['query']}: nessun match registrato"

    # match corretto (per nome)
    assert row["expect_match_contains"].lower() in matched.lower(), (
        f"{row['query']}: match '{matched}' non contiene '{row['expect_match_contains']}'"
    )
    # soglia sulla distanza (coseno)
    dist = row.get("distance")
    assert dist is not None and 0.0 <= dist <= row["max_distance"], (
        f"{row['query']}: distanza {dist} fuori soglia [0, {row['max_distance']}]"
    )
