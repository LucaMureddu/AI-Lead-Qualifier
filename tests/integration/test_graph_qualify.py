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

from core.graph import build_graph

pytestmark = pytest.mark.integration


class TestQualifyGraph:
    async def test_happy_path(self, make_lead_state, checkpointer) -> None:
        mapper = AsyncMock(return_value={
            "mapped_services": [
                {"matched_name": "Cloud Migration", "price": 3000.0, "unit": "€"},
                {"matched_name": "SEO Audit", "price": 500.0, "unit": "€"},
            ],
            "sse_logs": ["[MAPPER] mapped=2"],
            "error": None,
        })
        with patch("agents.extractor._call_openai_compatible",
                   new=AsyncMock(return_value='["Cloud Migration", "SEO Audit"]')), \
             patch("core.graph.mapper_node", new=mapper):
            graph = build_graph(checkpointer=checkpointer)
            final = await graph.ainvoke(
                make_lead_state(), config={"configurable": {"thread_id": "q-happy"}}
            )
        assert final["total_quote"] == 3500.0
        assert final["error"] is None
        assert final["delivery_status"] == "SUCCESS"   # ConsoleAdapter (no rete) conferma

    async def test_retry_then_human_fallback_suspends(self, make_lead_state, checkpointer) -> None:
        empty_mapper = AsyncMock(return_value={
            "mapped_services": [], "sse_logs": ["[MAPPER] mapped=0"], "error": None,
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
            "sse_logs": ["[MAPPER] mapped=1"], "error": None,
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
