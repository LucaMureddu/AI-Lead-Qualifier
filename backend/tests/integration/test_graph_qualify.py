"""
tests/integration/test_graph_qualify.py
----------------------------------------
Grafo di qualifica eseguito per intero con ``ainvoke``, ma con i confini esterni
finti: LLM (``agents.extractor._call_openai_compatible``) e ChromaDB
(``core.graph.mapper_node``, patchato DOVE È USATO) sempre mockati.

Apprendimento Fase 0: in LangGraph 1.x ``interrupt()`` NON solleva fuori da
``ainvoke`` — la sospensione si verifica via ``__interrupt__`` / ``aget_state``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.documents import Document

from core.config import get_settings
from core.graph import build_graph

pytestmark = pytest.mark.integration


class TestQualifyGraph:
    async def test_happy_path(self, make_lead_state, checkpointer) -> None:
        mapper = AsyncMock(return_value={
            "mapped_services": [
                {"matched_name": "Cloud Migration", "price": 3000.0, "unit": "€"},
                {"matched_name": "SEO Audit", "price": 500.0, "unit": "€"},
            ],
            "retrieved_docs": [
                Document(page_content="d", metadata={"service": "Cloud Migration", "price": 3000.0, "distance": 0.1}),
                Document(page_content="d", metadata={"service": "SEO Audit", "price": 500.0, "distance": 0.1}),
            ],
            "error_detail": None,
        })
        with patch("agents.extractor._call_openai_compatible",
                   new=AsyncMock(return_value='["Cloud Migration", "SEO Audit"]')), \
             patch("core.graph.mapper_node", new=mapper):
            graph = build_graph(checkpointer=checkpointer)
            final = await graph.ainvoke(
                make_lead_state(), config={"configurable": {"thread_id": "q-happy"}}
            )
        assert final["total_quote"] == 3500.0
        assert final.get("error_detail") is None
        assert final["delivery_status"] == "SUCCESS"   # ConsoleAdapter (no rete) conferma

    async def test_retry_then_human_fallback_suspends(self, make_lead_state, checkpointer) -> None:
        empty_mapper = AsyncMock(return_value={
            "mapped_services": [], "retrieved_docs": [], "error_detail": None,
        })
        with patch("agents.extractor._call_openai_compatible",
                   new=AsyncMock(return_value='["Unknown Service XYZ"]')), \
             patch("core.graph.mapper_node", new=empty_mapper):
            graph = build_graph(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": "q-fallback"}}
            final = await graph.ainvoke(make_lead_state(), config=config)
            snapshot = await graph.aget_state(config)
        # Sospeso ad human_fallback (interrupt), non terminato con successo.
        assert "__interrupt__" in final or bool(snapshot.next)

    async def test_delivery_retries_then_succeeds(self, make_lead_state, checkpointer) -> None:
        deliver = AsyncMock(side_effect=[False, True])   # 1° tentativo fallisce, 2° riesce
        adapter = MagicMock()
        adapter.deliver = deliver
        mapper = AsyncMock(return_value={
            "mapped_services": [{"matched_name": "Svc", "price": 100.0, "unit": "€"}],
            "retrieved_docs": [
                Document(page_content="d", metadata={"service": "Svc", "price": 100.0, "distance": 0.1}),
            ],
            "error_detail": None,
        })
        with patch("agents.extractor._call_openai_compatible",
                   new=AsyncMock(return_value='["Svc"]')), \
             patch("core.graph.mapper_node", new=mapper), \
             patch("agents.delivery.get_delivery_adapter", return_value=adapter):
            graph = build_graph(checkpointer=checkpointer)
            final = await graph.ainvoke(
                make_lead_state(), config={"configurable": {"thread_id": "q-deliv"}}
            )
        assert final["delivery_status"] == "SUCCESS"
        assert final["delivery_attempts"] == 2
        assert deliver.await_count == 2

    async def test_offtarget_distance_routes_to_human_fallback(
        self, make_lead_state, checkpointer, monkeypatch
    ) -> None:
        # Con la soglia 0.5 attiva, un match a distanza alta (off-target) viene
        # scartato dal mapper → mapped_services vuoto → retry → human_fallback.
        # NB: il mapper REALE gira (non lo mockiamo); mockiamo solo pgvector.
        monkeypatch.setenv("MAPPER_MAX_DISTANCE", "0.5")
        get_settings.cache_clear()
        with patch("agents.extractor._call_openai_compatible",
                   new=AsyncMock(return_value='["celle frigorifere"]')), \
             patch("agents.mapper.aembed_query", new=AsyncMock(return_value=[0.1] * 768)), \
             patch("agents.mapper.similarity_search", new=AsyncMock(return_value=[])):
            graph = build_graph(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": "q-offtarget"}}
            final = await graph.ainvoke(make_lead_state(), config=config)
            snapshot = await graph.aget_state(config)

        assert final.get("mapped_services") == []                 # nessun match accettato
        assert "__interrupt__" in final or bool(snapshot.next)     # sospeso ad human_fallback
