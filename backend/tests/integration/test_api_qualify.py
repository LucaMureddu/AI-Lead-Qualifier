"""
tests/integration/test_api_qualify.py
--------------------------------------
Endpoint /health e /qualify (sync + stream) via httpx.ASGITransport.
Nessun server avviato, LLM/Chroma mockati (patch dove sono usati).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

_GOOD_TEXT = "Vorrei un sito web aziendale completo e un audit SEO"


def _mapper(services: list[dict]) -> AsyncMock:
    return AsyncMock(return_value={"mapped_services": services, "retrieved_docs": [], "error_detail": None})


async def test_health(api_client) -> None:
    r = await api_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_qualify_raw_text_too_short_422(api_client) -> None:
    r = await api_client.post("/qualify", json={"raw_text": "ciao"})
    assert r.status_code == 422  # min_length=10 su raw_text


async def test_qualify_success_200(api_client) -> None:
    mapper = _mapper([
        {"matched_name": "Cloud Migration", "price": 3000.0, "unit": "€"},
        {"matched_name": "SEO Audit", "price": 500.0, "unit": "€"},
    ])
    with patch("agents.extractor._call_openai_compatible",
               new=AsyncMock(return_value='["Cloud Migration", "SEO Audit"]')), \
         patch("core.graph.mapper_node", new=mapper):
        r = await api_client.post("/qualify", json={"raw_text": _GOOD_TEXT})
    assert r.status_code == 200
    body = r.json()
    assert body["total_quote"] == 3500.0
    assert len(body["mapped_services"]) == 2
    assert body.get("error_detail") is None


async def test_qualify_graph_error_500(api_client) -> None:
    failing = MagicMock()
    failing.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("api.routes.build_graph", return_value=failing):
        r = await api_client.post("/qualify", json={"raw_text": _GOOD_TEXT})
    assert r.status_code == 500


async def test_qualify_stream_emits_log_then_done(api_client) -> None:
    mapper = _mapper([{"matched_name": "Svc", "price": 100.0, "unit": "€"}])
    with patch("agents.extractor._call_openai_compatible", new=AsyncMock(return_value='["Svc"]')), \
         patch("core.graph.mapper_node", new=mapper):
        r = await api_client.post("/qualify/stream", json={"raw_text": _GOOD_TEXT})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "event: log" in body
    assert "event: done" in body


async def test_qualify_stream_emits_error_on_exception(api_client) -> None:
    # mapper_node che solleva → astream propaga → il generator emette event: error
    with patch("agents.extractor._call_openai_compatible", new=AsyncMock(return_value='["Svc"]')), \
         patch("core.graph.mapper_node", new=AsyncMock(side_effect=RuntimeError("boom"))):
        r = await api_client.post("/qualify/stream", json={"raw_text": _GOOD_TEXT})
    assert r.status_code == 200
    assert "event: error" in r.text
