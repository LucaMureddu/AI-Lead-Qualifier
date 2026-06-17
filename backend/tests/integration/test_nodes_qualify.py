"""
tests/integration/test_nodes_qualify.py
----------------------------------------
Test dei singoli nodi del grafo di qualifica, ognuno isolato mockando SOLO la
sua dipendenza esterna (LLM / ChromaDB / adapter). Nessun servizio reale.

Regola d'oro (TESTING_PLAN.md §3.5): si patcha il nome DOVE È USATO.
- extractor → ``agents.extractor._call_openai_compatible`` (httpx, via respx).
- mapper    → ``agents.mapper._query_chroma_sync`` (chiamato in asyncio.to_thread).
- delivery  → ``agents.delivery.get_delivery_adapter`` (legato nel namespace del nodo).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

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
        assert out["error"] is None

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
        assert out["error"] is not None

    async def test_empty_sanitized_text_short_circuits(self, make_lead_state) -> None:
        # Nessuna chiamata LLM attesa → niente respx.
        out = await extractor_node(make_lead_state(sanitized_text=""))
        assert out["extracted_services"] == []
        assert out["error"] is not None

    async def test_retry_feeds_previous_services_into_prompt(self, make_lead_state, respx_mock) -> None:
        respx_mock.post(_llm_url()).mock(return_value=_chat_response('["Refined Service"]'))
        state = make_lead_state(
            sanitized_text="x", retry_count=1, extracted_services=["Old Service"]
        )
        out = await extractor_node(state)
        assert out["retry_count"] == 2
        body = respx_mock.calls.last.request.content.decode("utf-8")
        assert "Old Service" in body  # il feedback negativo è nel prompt


# ── Mapper (mock di _query_chroma_sync) ────────────────────────────────────────

class TestMapperNode:
    async def test_best_match_selected(self, make_lead_state) -> None:
        fake_result = {
            "ids": [["id-1"]],
            "documents": [["Cloud Migration doc"]],
            "metadatas": [[{"service_name": "Cloud Migration", "price": 3000.0, "unit": "€"}]],
            "distances": [[0.12]],
        }
        with patch("agents.mapper._query_chroma_sync", return_value=fake_result):
            out = await mapper_node(make_lead_state(extracted_services=["Cloud"]))
        assert out["error"] is None
        assert len(out["mapped_services"]) == 1
        match = out["mapped_services"][0]
        assert match["matched_name"] == "Cloud Migration"
        assert match["price"] == 3000.0
        assert match["distance"] == 0.12

    async def test_missing_collection_value_error_handled(self, make_lead_state) -> None:
        with patch("agents.mapper._query_chroma_sync", side_effect=ValueError("no collection")):
            out = await mapper_node(make_lead_state(extracted_services=["X"]))
        assert out["mapped_services"] == []
        assert "Run /ingest/stream first" in out["error"]

    async def test_generic_error_handled(self, make_lead_state) -> None:
        with patch("agents.mapper._query_chroma_sync", side_effect=RuntimeError("boom")):
            out = await mapper_node(make_lead_state(extracted_services=["X"]))
        assert out["mapped_services"] == []
        assert out["error"] is not None

    async def test_no_extracted_services_returns_empty(self, make_lead_state) -> None:
        out = await mapper_node(make_lead_state(extracted_services=[]))
        assert out["mapped_services"] == []
        assert "sse_logs" in out


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


# ── Coverage extra: best-match assente e _query_chroma_sync mockato ────────────

async def test_mapper_no_match_when_empty_metadata(make_lead_state) -> None:
    empty = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
    with patch("agents.mapper._query_chroma_sync", return_value=empty):
        out = await mapper_node(make_lead_state(extracted_services=["X"]))
    assert out["mapped_services"] == []
    assert out["error"] is None


def test_query_chroma_sync_uses_collection() -> None:
    """Copre _query_chroma_sync con chromadb.HttpClient mockato (no rete)."""
    from agents.mapper import _query_chroma_sync

    collection = MagicMock()
    collection.query.return_value = {
        "ids": [["a"]], "documents": [["d"]], "metadatas": [[{}]], "distances": [[0.1]],
    }
    client = MagicMock()
    client.get_or_create_collection.return_value = collection
    with patch("chromadb.HttpClient", return_value=client):
        res = _query_chroma_sync("localhost", 8001, "catalogue_acme", ["x"], 3)

    assert res["ids"] == [["a"]]
    client.get_or_create_collection.assert_called_once_with(name="catalogue_acme")
    collection.query.assert_called_once()


async def test_extractor_generic_exception_handled(make_lead_state) -> None:
    # Un'eccezione non-httpx è catturata dall'except generico di extractor_node.
    with patch("agents.extractor._call_openai_compatible", new=AsyncMock(side_effect=ValueError("weird"))):
        out = await extractor_node(make_lead_state(sanitized_text="x", retry_count=0))
    assert out["extracted_services"] == []
    assert out["retry_count"] == 1
    assert out["error"] is not None


# ── Mapper: sbarramento per distanza (mapper_max_distance) ─────────────────────

def _chroma_result(distance: float) -> dict:
    return {
        "ids": [["svc-x"]],
        "documents": [["Doc X"]],
        "metadatas": [[{"service_name": "X", "price": 1.0, "unit": "€"}]],
        "distances": [[distance]],
    }


async def test_mapper_drops_match_above_distance_threshold(make_lead_state, monkeypatch) -> None:
    monkeypatch.setenv("MAPPER_MAX_DISTANCE", "0.5")
    get_settings.cache_clear()
    with patch("agents.mapper._query_chroma_sync", return_value=_chroma_result(0.9)):
        out = await mapper_node(make_lead_state(extracted_services=["celle frigorifere"]))
    assert out["mapped_services"] == []   # match oltre soglia → scartato (off-target)
    assert out["error"] is None


async def test_mapper_keeps_match_below_distance_threshold(make_lead_state, monkeypatch) -> None:
    monkeypatch.setenv("MAPPER_MAX_DISTANCE", "0.5")
    get_settings.cache_clear()
    with patch("agents.mapper._query_chroma_sync", return_value=_chroma_result(0.3)):
        out = await mapper_node(make_lead_state(extracted_services=["sito web"]))
    assert len(out["mapped_services"]) == 1   # sotto soglia → tenuto


async def test_mapper_threshold_disabled_keeps_far_match(make_lead_state, monkeypatch) -> None:
    # default mapper_max_distance=0.0 → disabilitato → tiene anche un match lontano.
    monkeypatch.delenv("MAPPER_MAX_DISTANCE", raising=False)
    get_settings.cache_clear()
    with patch("agents.mapper._query_chroma_sync", return_value=_chroma_result(0.9)):
        out = await mapper_node(make_lead_state(extracted_services=["x"]))
    assert len(out["mapped_services"]) == 1   # soglia disattiva → comportamento storico
