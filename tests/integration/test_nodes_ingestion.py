"""
tests/integration/test_nodes_ingestion.py
------------------------------------------
Test dei singoli nodi del grafo di ingestion, isolati con mock mirati.
- chunker   → legge file reali da tmp_path (CSV/JSON), nessuna rete.
- normalizer→ ``agents.extractor._call_openai_compatible`` (import locale nel nodo).
- validator → puro, nessun mock.
- finalizer → ``ingestion.graph._write_to_chroma_sync`` (chiamato in asyncio.to_thread).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ingestion.graph import (
    chunker_node,
    finalizer_node,
    normalizer_node,
    validator_node,
)
from ingestion.models import ServiceItem

pytestmark = pytest.mark.integration


def _ing_state(**ovr) -> dict:
    """IngestionState minimale per i test diretti dei nodi."""
    base = {
        "tenant_id": "acme",
        "source_file": "",
        "file_format": "csv",
        "raw_chunks": [],
        "current_chunk_index": 0,
        "normalized_items": [],
        "validation_errors": [],
        "flagged_items": [],
        "confidence_score": 0.0,
        "approved": None,
        "review_feedback": None,
        "sse_logs": [],
        "error": None,
    }
    base.update(ovr)
    return base


# ── Chunker (lettura file da disco, no rete) ───────────────────────────────────

class TestChunkerNode:
    async def test_reads_csv(self, tmp_path) -> None:
        f = tmp_path / "c.csv"
        f.write_text("name,price\nA,10\nB,20\n", encoding="utf-8")
        out = await chunker_node(_ing_state(source_file=str(f), file_format="csv"))
        assert out["error"] is None
        assert len(out["raw_chunks"]) == 1
        assert len(out["raw_chunks"][0]) == 2  # due righe dati
        assert out["current_chunk_index"] == 0

    async def test_reads_json(self, tmp_path) -> None:
        f = tmp_path / "c.json"
        f.write_text('[{"name": "A", "price": 10}]', encoding="utf-8")
        out = await chunker_node(_ing_state(source_file=str(f), file_format="json"))
        assert out["error"] is None
        assert len(out["raw_chunks"][0]) == 1

    async def test_file_not_found(self) -> None:
        out = await chunker_node(_ing_state(source_file="/nope/missing.csv", file_format="csv"))
        assert out["error"] is not None
        assert out["raw_chunks"] == []

    async def test_unsupported_format(self, tmp_path) -> None:
        f = tmp_path / "c.txt"
        f.write_text("qualcosa", encoding="utf-8")
        out = await chunker_node(_ing_state(source_file=str(f), file_format="txt"))
        assert out["error"] is not None


# ── Normalizer (mock LLM via agents.extractor._call_openai_compatible) ─────────

class TestNormalizerNode:
    async def test_success_parses_items(self) -> None:
        resp = '[{"name": "Sviluppo Sito", "description": "sito", "price": 1000, "currency": "EUR", "confidence": 0.95}]'
        with patch("agents.extractor._call_openai_compatible", new=AsyncMock(return_value=resp)):
            out = await normalizer_node(_ing_state(raw_chunks=[[{"name": "raw"}]], current_chunk_index=0))
        assert out["current_chunk_index"] == 1
        assert len(out["normalized_items"]) == 1
        assert out["normalized_items"][0].name == "Sviluppo Sito"
        assert out["error"] is None

    async def test_malformed_row_creates_flagged_placeholder(self) -> None:
        # price negativo → ServiceItem solleva ValidationError → placeholder flaggato (nessun dato perso)
        resp = '[{"name": "Bad", "price": -5}]'
        with patch("agents.extractor._call_openai_compatible", new=AsyncMock(return_value=resp)):
            out = await normalizer_node(_ing_state(raw_chunks=[[{"name": "raw-bad"}]], current_chunk_index=0))
        assert len(out["normalized_items"]) == 1
        item = out["normalized_items"][0]
        assert item.flagged is True
        assert item.flag_reason and "Construction error" in item.flag_reason

    async def test_chunk_index_advances(self) -> None:
        with patch("agents.extractor._call_openai_compatible", new=AsyncMock(return_value="[]")):
            out = await normalizer_node(_ing_state(raw_chunks=[[{}], [{}]], current_chunk_index=0))
        assert out["current_chunk_index"] == 1

    async def test_index_out_of_range_guard(self) -> None:
        # Nessuna chiamata LLM: il guard ritorna prima.
        out = await normalizer_node(_ing_state(raw_chunks=[], current_chunk_index=5))
        assert out["current_chunk_index"] == 6

    async def test_llm_error_sets_error(self) -> None:
        with patch("agents.extractor._call_openai_compatible", new=AsyncMock(side_effect=RuntimeError("boom"))):
            out = await normalizer_node(_ing_state(raw_chunks=[[{"name": "raw"}]], current_chunk_index=0))
        assert out["error"] is not None
        assert out["current_chunk_index"] == 1


# ── Validator (puro, regole di business) ───────────────────────────────────────

class TestValidatorNode:
    async def test_flags_zero_price_without_description(self) -> None:
        item = ServiceItem(tenant_id="acme", name="Servizio", price=0.0, confidence=0.9)
        out = await validator_node(_ing_state(normalized_items=[item]))
        assert out["confidence_score"] == 0.9
        assert len(out["flagged_items"]) == 1
        assert any("zero price" in e for e in out["validation_errors"])

    async def test_flags_short_name(self) -> None:
        item = ServiceItem(tenant_id="acme", name="ok", description="d", price=10.0, confidence=0.9)
        out = await validator_node(_ing_state(normalized_items=[item]))
        assert len(out["flagged_items"]) == 1

    async def test_flags_unknown_unit(self) -> None:
        item = ServiceItem(
            tenant_id="acme", name="Servizio", description="d", price=10.0, unit="parsec", confidence=0.9
        )
        out = await validator_node(_ing_state(normalized_items=[item]))
        assert len(out["flagged_items"]) == 1

    async def test_clean_item_not_flagged(self) -> None:
        item = ServiceItem(
            tenant_id="acme", name="Servizio Buono", description="ok", price=100.0, unit="hour", confidence=0.95
        )
        out = await validator_node(_ing_state(normalized_items=[item]))
        assert out["flagged_items"] == []
        assert out["confidence_score"] == 0.95


# ── Finalizer (mock di _write_to_chroma_sync) ──────────────────────────────────

class TestFinalizerNode:
    async def test_dedup_by_id(self) -> None:
        a1 = ServiceItem(id="A", tenant_id="acme", name="S1", price=10.0, confidence=0.9)
        a2 = ServiceItem(id="A", tenant_id="acme", name="S1-dup", price=10.0, confidence=0.9)
        b = ServiceItem(id="B", tenant_id="acme", name="S2", price=20.0, confidence=0.9)
        writer = MagicMock(return_value=2)
        with patch("ingestion.graph._write_to_chroma_sync", writer):
            out = await finalizer_node(_ing_state(normalized_items=[a1, a2, b], approved=True))
        assert out["error"] is None
        written_items = writer.call_args.args[3]  # (host, port, tenant_id, items)
        assert {i.id for i in written_items} == {"A", "B"}
        assert len(written_items) == 2

    async def test_write_error_sets_error(self) -> None:
        item = ServiceItem(id="A", tenant_id="acme", name="S1", price=10.0, confidence=0.9)
        with patch("ingestion.graph._write_to_chroma_sync", MagicMock(side_effect=RuntimeError("boom"))):
            out = await finalizer_node(_ing_state(normalized_items=[item]))
        assert out["error"] is not None

    async def test_success_logs_written(self) -> None:
        item = ServiceItem(id="A", tenant_id="acme", name="S1", price=10.0, confidence=0.9)
        with patch("ingestion.graph._write_to_chroma_sync", MagicMock(return_value=1)):
            out = await finalizer_node(_ing_state(normalized_items=[item]))
        assert out["error"] is None
        assert any("FINALIZER" in log for log in out["sse_logs"])


# ── Coverage extra: reader JSON, feedback del normalizer, write su Chroma ──────

async def test_chunker_reads_json_dict_wrapper(tmp_path) -> None:
    f = tmp_path / "w.json"
    f.write_text('{"items": [{"name": "A", "price": 1}]}', encoding="utf-8")
    out = await chunker_node(_ing_state(source_file=str(f), file_format="json"))
    assert out["error"] is None
    assert len(out["raw_chunks"][0]) == 1


async def test_chunker_json_invalid_object_errors(tmp_path) -> None:
    f = tmp_path / "bad.json"
    f.write_text('{"unknown_key": 1}', encoding="utf-8")
    out = await chunker_node(_ing_state(source_file=str(f), file_format="json"))
    assert out["error"] is not None


async def test_normalizer_injects_review_feedback() -> None:
    captured: dict = {}

    async def fake(system: str, user: str) -> str:
        captured["user"] = user
        return '[{"name": "S", "description": "d", "price": 10, "currency": "EUR", "confidence": 0.9}]'

    with patch("agents.extractor._call_openai_compatible", new=fake):
        out = await normalizer_node(_ing_state(
            raw_chunks=[[{"name": "raw"}]],
            current_chunk_index=0,
            review_feedback="correggi i prezzi",
        ))
    assert "correggi i prezzi" in captured["user"]   # feedback iniettato nel prompt
    assert out["error"] is None


def test_write_to_chroma_sync_builds_and_upserts() -> None:
    """Copre _write_to_chroma_sync con chromadb.HttpClient mockato (no rete)."""
    from ingestion.graph import _write_to_chroma_sync

    item = ServiceItem(
        id="A", tenant_id="acme", name="S1", description="d", category="cat",
        price=10.0, unit="hour", confidence=0.9,
    )
    collection = MagicMock()
    client = MagicMock()
    client.get_or_create_collection.return_value = collection
    with patch("chromadb.HttpClient", return_value=client):
        written = _write_to_chroma_sync("localhost", 8001, "acme", [item])

    assert written == 1
    client.get_or_create_collection.assert_called_once()
    collection.upsert.assert_called_once()
    assert collection.upsert.call_args.kwargs["ids"] == ["A"]
