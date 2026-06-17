"""
tests/integration/test_graph_ingestion.py
------------------------------------------
Grafo di ingestion eseguito per intero con confini esterni mockati:
- LLM del normalizer → ``agents.extractor._call_openai_compatible``
- scrittura ChromaDB → ``ingestion.graph._write_to_chroma_sync``

Copre: loop sui chunk, routing ad approval su catalogo problematico (HITL),
e resume con ``Command(resume={...})`` sia approvato che rifiutato.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.types import Command

from core.config import get_settings
from ingestion.graph import build_ingestion_graph, make_initial_state

pytestmark = pytest.mark.integration

# catalogo_problematico.csv è nella root del progetto (parents[2] da questo file).
_PROBLEMATIC_CSV = Path(__file__).resolve().parents[2] / "catalogo_problematico.csv"

# Output del normalizer che provoca il flag (HITL): nome cortissimo, prezzo 0,
# confidence < 0.5 (auto-flag) → route_after_validator → approval.
_FLAGGED_LLM_JSON = '[{"name": "??", "price": 0, "currency": "EUR", "confidence": 0.3}]'


class TestIngestionChunkLoop:
    async def test_processes_all_chunks_then_finalizes(self, tmp_path, checkpointer, monkeypatch) -> None:
        # 60 righe → CHUNK_SIZE=50 → 2 chunk → normalizer chiamato 2 volte.
        monkeypatch.setenv("INGESTION_CHUNK_SIZE", "50")
        get_settings.cache_clear()
        rows = "name,price\n" + "".join(f"S{i},{i + 1}\n" for i in range(60))
        f = tmp_path / "big.csv"
        f.write_text(rows, encoding="utf-8")

        clean_item = (
            '[{"name": "Servizio Valido", "description": "ok", "price": 100, '
            '"currency": "EUR", "confidence": 0.95}]'
        )
        llm = AsyncMock(return_value=clean_item)
        writer = MagicMock(return_value=2)
        with patch("agents.extractor._call_openai_compatible", new=llm), \
             patch("ingestion.graph._write_to_chroma_sync", writer):
            graph = build_ingestion_graph(checkpointer)
            state = make_initial_state(tenant_id="acme", source_file=str(f), file_format="csv")
            final = await graph.ainvoke(state, config={"configurable": {"thread_id": "ing-loop"}})

        assert llm.await_count == 2                     # un chunk per chiamata (50 + 10)
        assert len(final["normalized_items"]) == 2      # operator.add accumula
        assert final["error"] is None
        assert writer.called                            # run pulito → finalizer scrive


class TestIngestionApprovalHITL:
    async def test_problematic_catalog_routes_to_approval(self, checkpointer) -> None:
        llm = AsyncMock(return_value=_FLAGGED_LLM_JSON)
        with patch("agents.extractor._call_openai_compatible", new=llm):
            graph = build_ingestion_graph(checkpointer)
            config = {"configurable": {"thread_id": "ing-approval"}}
            state = make_initial_state(
                tenant_id="acme", source_file=str(_PROBLEMATIC_CSV), file_format="csv"
            )
            final = await graph.ainvoke(state, config=config)
            snapshot = await graph.aget_state(config)
        # Sospeso ad approval (HITL): NON usare pytest.raises(GraphInterrupt).
        assert "__interrupt__" in final or bool(snapshot.next)

    async def test_resume_approved_runs_finalizer(self, checkpointer) -> None:
        llm = AsyncMock(return_value=_FLAGGED_LLM_JSON)
        writer = MagicMock(return_value=1)
        with patch("agents.extractor._call_openai_compatible", new=llm), \
             patch("ingestion.graph._write_to_chroma_sync", writer):
            graph = build_ingestion_graph(checkpointer)
            config = {"configurable": {"thread_id": "ing-resume-ok"}}
            state = make_initial_state(
                tenant_id="acme", source_file=str(_PROBLEMATIC_CSV), file_format="csv"
            )
            await graph.ainvoke(state, config=config)                 # sospende ad approval
            result = await graph.ainvoke(
                Command(resume={"approved": True, "feedback": "ok"}), config=config
            )
        assert result.get("approved") is True
        assert writer.called   # dopo l'approvazione il finalizer scrive

    async def test_resume_rejected_skips_finalizer(self, checkpointer) -> None:
        llm = AsyncMock(return_value=_FLAGGED_LLM_JSON)
        writer = MagicMock(return_value=1)
        with patch("agents.extractor._call_openai_compatible", new=llm), \
             patch("ingestion.graph._write_to_chroma_sync", writer):
            graph = build_ingestion_graph(checkpointer)
            config = {"configurable": {"thread_id": "ing-resume-no"}}
            state = make_initial_state(
                tenant_id="acme", source_file=str(_PROBLEMATIC_CSV), file_format="csv"
            )
            await graph.ainvoke(state, config=config)
            result = await graph.ainvoke(
                Command(resume={"approved": False, "feedback": "rivedere i prezzi"}), config=config
            )
        assert result.get("approved") is False
        assert not writer.called   # rifiutato → finalizer NON eseguito
