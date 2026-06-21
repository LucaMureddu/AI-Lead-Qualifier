"""
tests/integration/test_nodes_qualify.py
----------------------------------------
Test dei singoli nodi del grafo di qualifica, ognuno isolato mockando SOLO la
sua dipendenza esterna (LLM / ChromaDB / adapter). Nessun servizio reale.

Regola d'oro (TESTING_PLAN.md §3.5): si patcha il nome DOVE È USATO.
- extractor → ``agents.extractor._call_openai_compatible`` (httpx, via respx).
- mapper    → ``agents.mapper.aembed_query`` + ``agents.mapper.similarity_search`` (pgvector).
- delivery  → ``agents.delivery.get_delivery_adapter`` (legato nel namespace del nodo).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from langchain_core.documents import Document

from agents.delivery import delivery_node
from agents.extractor import extractor_node
from agents.mapper import mapper_node
from core.config import get_settings

pytestmark = pytest.mark.integration


def _llm_url() -> str:
    return f"{get_settings().llm_base_url.rstrip('/')}/chat/completions"


def _chat_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


# ── Extractor (respx sull'endpoint OpenAI-compatibile) ─────────────────────────

class TestExtractorNode:
    async def test_parses_clean_json(self, make_lead_state, respx_mock) -> None:
        respx_mock.post(_llm_url()).mock(
            return_value=_chat_response('["Web Development", "SEO Audit"]')
        )
        out = await extractor_node(make_lead_state(sanitized_text="Vorrei un sito e un audit SEO"))
        assert out["extracted_services"] == ["Web Development", "SEO Audit"]
        assert out["retry_count"] == 1
        assert out.get("error_detail") is None

    async def test_parses_markdown_fenced_json(self, make_lead_state, respx_mock) -> None:
        respx_mock.post(_llm_url()).mock(return_value=_chat_response('```json\n["A", "B"]\n```'))
        out = await extractor_node(make_lead_state(sanitized_text="x"))
        assert out["extracted_services"] == ["A", "B"]

    async def test_malformed_json_returns_empty(self, make_lead_state, respx_mock) -> None:
        respx_mock.post(_llm_url()).mock(return_value=_chat_response("non sono affatto JSON"))
        out = await extractor_node(make_lead_state(sanitized_text="x"))
        assert out["extracted_services"] == []

    async def test_http_error_sets_error_and_increments_retry(self, make_lead_state, respx_mock) -> None:
        respx_mock.post(_llm_url()).mock(return_value=httpx.Response(500, text="boom"))
        out = await extractor_node(make_lead_state(sanitized_text="x", retry_count=0))
        assert out["extracted_services"] == []
        assert out["retry_count"] == 1
        assert out.get("error_detail") is not None

    async def test_empty_sanitized_text_short_circuits(self, make_lead_state) -> None:
        # Nessuna chiamata LLM attesa → niente respx.
        out = await extractor_node(make_lead_state(sanitized_text=""))
        assert out["extracted_services"] == []
        assert out.get("error_detail") is not None

    async def test_retry_feeds_previous_services_into_prompt(self, make_lead_state, respx_mock) -> None:
        respx_mock.post(_llm_url()).mock(return_value=_chat_response('["Refined Service"]'))
        state = make_lead_state(
            sanitized_text="x", retry_count=1, extracted_services=["Old Service"]
        )
        out = await extractor_node(state)
        assert out["retry_count"] == 2
        body = respx_mock.calls.last.request.content.decode("utf-8")
        assert "Old Service" in body  # il feedback negativo è nel prompt


# ── Mapper (mock di aembed_query + similarity_search) ─────────────────────────

_FAKE_EMBEDDING = [0.1] * 768


class TestMapperNode:
    async def test_best_match_selected(self, make_lead_state) -> None:
        fake_docs = [Document(
            page_content="Cloud Migration doc",
            metadata={"service": "Cloud Migration", "price": 3000.0, "price_type": "FIXED", "unit": "€", "distance": 0.12},
        )]
        with patch("agents.mapper.aembed_query", new=AsyncMock(return_value=_FAKE_EMBEDDING)), \
             patch("agents.mapper.similarity_search", new=AsyncMock(return_value=fake_docs)):
            out = await mapper_node(make_lead_state(extracted_services=["Cloud"]))
        assert out.get("error_detail") is None
        assert len(out["mapped_services"]) == 1
        match = out["mapped_services"][0]
        assert match["matched_name"] == "Cloud Migration"
        assert match["price"] == 3000.0
        assert match["distance"] == 0.12

    async def test_missing_collection_value_error_handled(self, make_lead_state) -> None:
        with patch("agents.mapper.aembed_query", new=AsyncMock(return_value=_FAKE_EMBEDDING)), \
             patch("agents.mapper.similarity_search", new=AsyncMock(side_effect=ValueError("no collection"))):
            out = await mapper_node(make_lead_state(extracted_services=["X"]))
        assert out["mapped_services"] == []
        assert "Run /ingest/stream first" in out["error_detail"]

    async def test_generic_error_handled(self, make_lead_state) -> None:
        with patch("agents.mapper.aembed_query", new=AsyncMock(return_value=_FAKE_EMBEDDING)), \
             patch("agents.mapper.similarity_search", new=AsyncMock(side_effect=RuntimeError("boom"))):
            out = await mapper_node(make_lead_state(extracted_services=["X"]))
        assert out["mapped_services"] == []
        assert out.get("error_detail") is not None

    async def test_no_extracted_services_returns_empty(self, make_lead_state) -> None:
        out = await mapper_node(make_lead_state(extracted_services=[]))
        assert out["mapped_services"] == []
        assert "mapped_services" in out


# ── Delivery (mock dell'adapter) ───────────────────────────────────────────────

def _adapter(deliver: AsyncMock) -> MagicMock:
    a = MagicMock()
    a.deliver = deliver
    return a


class TestDeliveryNode:
    async def test_success(self, make_lead_state) -> None:
        with patch("agents.delivery.get_delivery_adapter", return_value=_adapter(AsyncMock(return_value=True))):
            out = await delivery_node(make_lead_state(delivery_attempts=0))
        assert out["delivery_status"] == "SUCCESS"
        assert out["delivery_attempts"] == 1
        assert out["delivery_error"] is None

    async def test_adapter_false_is_failed(self, make_lead_state) -> None:
        with patch("agents.delivery.get_delivery_adapter", return_value=_adapter(AsyncMock(return_value=False))):
            out = await delivery_node(make_lead_state(delivery_attempts=0))
        assert out["delivery_status"] == "FAILED"
        assert out["delivery_error"] is not None

    async def test_attempts_incremented(self, make_lead_state) -> None:
        with patch("agents.delivery.get_delivery_adapter", return_value=_adapter(AsyncMock(return_value=True))):
            out = await delivery_node(make_lead_state(delivery_attempts=2))
        assert out["delivery_attempts"] == 3

    async def test_request_error_is_failed_not_raised(self, make_lead_state) -> None:
        deliver = AsyncMock(side_effect=httpx.ConnectError("refused", request=None))
        with patch("agents.delivery.get_delivery_adapter", return_value=_adapter(deliver)):
            out = await delivery_node(make_lead_state(delivery_attempts=0))
        assert out["delivery_status"] == "FAILED"
        assert out["delivery_attempts"] == 1
        assert out["delivery_error"] is not None

    async def test_unexpected_error_is_failed(self, make_lead_state) -> None:
        deliver = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("agents.delivery.get_delivery_adapter", return_value=_adapter(deliver)):
            out = await delivery_node(make_lead_state(delivery_attempts=0))
        assert out["delivery_status"] == "FAILED"


# ── Coverage extra: nessun match e similarity_search mockato ──────────────────

async def test_mapper_no_match_when_empty_metadata(make_lead_state) -> None:
    with patch("agents.mapper.aembed_query", new=AsyncMock(return_value=_FAKE_EMBEDDING)), \
         patch("agents.mapper.similarity_search", new=AsyncMock(return_value=[])):
        out = await mapper_node(make_lead_state(extracted_services=["X"]))
    assert out["mapped_services"] == []
    assert out.get("error_detail") is None


async def test_similarity_search_called_with_embedding(make_lead_state) -> None:
    """Verifica che mapper_node chiami similarity_search con l'embedding generato."""
    fake_embedding = [0.42] * 768
    mock_search = AsyncMock(return_value=[])
    with patch("agents.mapper.aembed_query", new=AsyncMock(return_value=fake_embedding)), \
         patch("agents.mapper.similarity_search", new=mock_search):
        await mapper_node(make_lead_state(extracted_services=["web development"]))
    mock_search.assert_called_once()
    assert mock_search.call_args.kwargs["query_embedding"] == fake_embedding


async def test_extractor_generic_exception_handled(make_lead_state) -> None:
    # Un'eccezione non-httpx è catturata dall'except generico di extractor_node.
    with patch("agents.extractor._call_openai_compatible", new=AsyncMock(side_effect=ValueError("weird"))):
        out = await extractor_node(make_lead_state(sanitized_text="x", retry_count=0))
    assert out["extracted_services"] == []
    assert out["retry_count"] == 1
    assert out.get("error_detail") is not None


# ── Mapper: sbarramento per distanza (mapper_max_distance) ─────────────────────

def _pgvector_doc(distance: float) -> list:
    return [Document(
        page_content="Doc X",
        metadata={"service": "X", "price": 1.0, "price_type": "FIXED", "unit": "€", "distance": distance},
    )]


async def test_mapper_drops_match_above_distance_threshold(make_lead_state, monkeypatch) -> None:
    # Con max_distance=0.5, similarity_search filtra i match lontani → ritorna [].
    monkeypatch.setenv("MAPPER_MAX_DISTANCE", "0.5")
    get_settings.cache_clear()
    mock_search = AsyncMock(return_value=[])  # DB filtra il match off-target
    with patch("agents.mapper.aembed_query", new=AsyncMock(return_value=_FAKE_EMBEDDING)), \
         patch("agents.mapper.similarity_search", new=mock_search):
        out = await mapper_node(make_lead_state(extracted_services=["celle frigorifere"]))
    assert out["mapped_services"] == []
    assert mock_search.call_args.kwargs["max_distance"] == 0.5


async def test_mapper_keeps_match_below_distance_threshold(make_lead_state, monkeypatch) -> None:
    monkeypatch.setenv("MAPPER_MAX_DISTANCE", "0.5")
    get_settings.cache_clear()
    with patch("agents.mapper.aembed_query", new=AsyncMock(return_value=_FAKE_EMBEDDING)), \
         patch("agents.mapper.similarity_search", new=AsyncMock(return_value=_pgvector_doc(0.3))):
        out = await mapper_node(make_lead_state(extracted_services=["sito web"]))
    assert len(out["mapped_services"]) == 1


async def test_mapper_threshold_disabled_keeps_far_match(make_lead_state, monkeypatch) -> None:
    # mapper_max_distance=0.0 → disabilitato → max_distance=None passato a similarity_search.
    monkeypatch.delenv("MAPPER_MAX_DISTANCE", raising=False)
    get_settings.cache_clear()
    mock_search = AsyncMock(return_value=_pgvector_doc(0.9))
    with patch("agents.mapper.aembed_query", new=AsyncMock(return_value=_FAKE_EMBEDDING)), \
         patch("agents.mapper.similarity_search", new=mock_search):
        out = await mapper_node(make_lead_state(extracted_services=["x"]))
    assert len(out["mapped_services"]) == 1
    assert mock_search.call_args.kwargs["max_distance"] is None
