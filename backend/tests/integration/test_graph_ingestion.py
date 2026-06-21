"""
tests/integration/test_graph_ingestion.py
------------------------------------------
Grafo di ingestion eseguito per intero con confini esterni mockati:
- LLM del normalizer → ``agents.extractor._call_openai_compatible``
- scrittura pgvector → ``ingestion.graph._write_to_pgvector``
- download S3      → ``services.storage.download_file``  (V2.1)

Copre: loop sui chunk, routing ad approval su catalogo problematico (HITL),
e resume con ``Command(resume={...})`` sia approvato che rifiutato.

V2.1: chunker_node scarica il file da S3 tramite services.storage.download_file().
Tutti i test mockano download_file per restituire i byte del file locale
senza connettersi a MinIO.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langgraph.types import Command

from core.config import get_settings
from ingestion.graph import build_ingestion_graph, make_initial_state

pytestmark = pytest.mark.integration

# Output del normalizer che provoca il flag (HITL): nome cortissimo, prezzo 0,
# confidence < 0.5 (auto-flag) → route_after_validator → approval.
_FLAGGED_LLM_JSON = '[{"name": "??", "price": 0, "currency": "EUR", "confidence": 0.3}]'

# CSV minimale con un servizio "problematico" (usato come payload fittizio per
# download_file). Il contenuto reale non influisce sul risultato dei test perché
# il LLM è mockato a restituire _FLAGGED_LLM_JSON indipendentemente dall'input.
_PROBLEMATIC_CSV_BYTES = b"name,price,description\nServizio Problematico,0,troppo corto\n"


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
        writer = AsyncMock(return_value=2)
        with patch("agents.extractor._call_openai_compatible", new=llm), \
             patch("ingestion.graph._write_to_pgvector", writer), \
             patch("services.storage.download_file",
                   new=AsyncMock(return_value=f.read_bytes())):
            graph = build_ingestion_graph(checkpointer)
            state = make_initial_state(tenant_id="acme", source_file="acme/big.csv", file_format="csv")
            final = await graph.ainvoke(state, config={"configurable": {"thread_id": "ing-loop"}})

        assert llm.await_count == 2                     # un chunk per chiamata (50 + 10)
        assert len(final["normalized_items"]) == 2      # operator.add accumula
        assert final["error"] is None
        assert writer.called                            # run pulito → finalizer scrive


class TestIngestionApprovalHITL:
    async def test_problematic_catalog_routes_to_approval(self, checkpointer) -> None:
        llm = AsyncMock(return_value=_FLAGGED_LLM_JSON)
        with patch("agents.extractor._call_openai_compatible", new=llm), \
             patch("services.storage.download_file",
                   new=AsyncMock(return_value=_PROBLEMATIC_CSV_BYTES)):
            graph = build_ingestion_graph(checkpointer)
            config = {"configurable": {"thread_id": "ing-approval"}}
            state = make_initial_state(
                tenant_id="acme", source_file="acme/catalogo_problematico.csv", file_format="csv"
            )
            final = await graph.ainvoke(state, config=config)
            snapshot = await graph.aget_state(config)
        # Sospeso ad approval (HITL): NON usare pytest.raises(GraphInterrupt).
        assert "__interrupt__" in final or bool(snapshot.next)

    async def test_resume_approved_runs_finalizer(self, checkpointer) -> None:
        llm = AsyncMock(return_value=_FLAGGED_LLM_JSON)
        writer = AsyncMock(return_value=1)
        with patch("agents.extractor._call_openai_compatible", new=llm), \
             patch("ingestion.graph._write_to_pgvector", writer), \
             patch("services.storage.download_file",
                   new=AsyncMock(return_value=_PROBLEMATIC_CSV_BYTES)):
            graph = build_ingestion_graph(checkpointer)
            config = {"configurable": {"thread_id": "ing-resume-ok"}}
            state = make_initial_state(
                tenant_id="acme", source_file="acme/catalogo_problematico.csv", file_format="csv"
            )
            await graph.ainvoke(state, config=config)                 # sospende ad approval
            result = await graph.ainvoke(
                Command(resume={"approved": True, "feedback": "ok"}), config=config
            )
        assert result.get("approved") is True
        assert writer.called   # dopo l'approvazione il finalizer scrive

    async def test_resume_rejected_skips_finalizer(self, checkpointer) -> None:
        llm = AsyncMock(return_value=_FLAGGED_LLM_JSON)
        writer = AsyncMock(return_value=1)
        with patch("agents.extractor._call_openai_compatible", new=llm), \
             patch("ingestion.graph._write_to_pgvector", writer), \
             patch("services.storage.download_file",
                   new=AsyncMock(return_value=_PROBLEMATIC_CSV_BYTES)):
            graph = build_ingestion_graph(checkpointer)
            config = {"configurable": {"thread_id": "ing-resume-no"}}
            state = make_initial_state(
                tenant_id="acme", source_file="acme/catalogo_problematico.csv", file_format="csv"
            )
            await graph.ainvoke(state, config=config)
            result = await graph.ainvoke(
                Command(resume={"approved": False, "feedback": "rivedere i prezzi"}), config=config
            )
        assert result.get("approved") is False
        assert not writer.called   # rifiutato → finalizer NON eseguito
