"""
tests/evals/test_mapping_eval.py
--------------------------------
BINARIO B — eval LIVE per il Mapper (marker ``eval``, escluso da ``-m "not eval"``).
Semina un mini-catalogo in pgvector e verifica il retrieval reale:

    pytest -m eval        # richiede pgvector + Ollama attivi

Copre i match positivi (categoria giusta + distanza sotto soglia stretta) e un
caso OFF-TARGET (query fuori catalogo → distanza alta). Se pgvector/Ollama non
sono raggiungibili, i test del mapper si SALTANO.

V2: migrato da ChromaDB a pgvector — coerente con la migrazione del mapper.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tests.evals._mapper import run_mapper, seed_catalog

pytestmark = pytest.mark.eval

_HERE = pathlib.Path(__file__).resolve().parent
_GOLDEN = [
    json.loads(line)
    for line in (_HERE / "datasets" / "mappings_golden.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]


@pytest.fixture()
async def seeded_catalog():
    """Semina il catalogo di eval in pgvector. Skip se pgvector/Ollama non raggiungibili."""
    try:
        await seed_catalog()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"pgvector/Ollama non raggiungibile — eval mapper saltati: {exc}")
    return True


@pytest.mark.parametrize("row", _GOLDEN, ids=[r["query"] for r in _GOLDEN])
async def test_mapping_live(row: dict, seeded_catalog) -> None:
    mapped = await run_mapper([row["query"]])
    assert mapped, f"{row['query']}: nessun mapped_service"
    best = mapped[0]
    dist = best["distance"]

    # ── Off-target: la distanza del miglior match deve essere ALTA ────────────
    if row.get("off_target"):
        assert dist > row["min_distance"], (
            f"{row['query']}: distanza {dist:.3f} troppo BASSA per un off-target "
            f"(soglia {row['min_distance']}) → rischio match allucinato"
        )
        return

    # ── Match positivo ────────────────────────────────────────────────────────
    assert row["expect_match_contains"].lower() in best["matched_name"].lower(), (
        f"{row['query']}: match '{best['matched_name']}' non contiene '{row['expect_match_contains']}'"
    )
    assert dist <= row["max_distance"], (
        f"{row['query']}: distanza {dist:.3f} oltre la soglia {row['max_distance']}"
    )
