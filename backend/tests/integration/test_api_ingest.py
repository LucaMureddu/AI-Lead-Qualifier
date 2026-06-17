"""
tests/integration/test_api_ingest.py
-------------------------------------
Endpoint /ingest/stream (header X-Thread-Id + event interrupt) e
/ingest/{thread_id}/approve (404 e 200) via ASGITransport.

Per /approve si mocka il grafo (``api.routes.build_ingestion_graph``) così da
isolare la logica dell'handler dalle meccaniche di resume di LangGraph (testate
a livello di grafo in test_graph_ingestion.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

_PROBLEMATIC_CSV = Path(__file__).resolve().parents[2] / "catalogo_problematico.csv"
_FLAGGED = '[{"name": "??", "price": 0, "currency": "EUR", "confidence": 0.3}]'


async def test_ingest_stream_header_and_interrupt(api_client) -> None:
    with patch("agents.extractor._call_openai_compatible", new=AsyncMock(return_value=_FLAGGED)):
        r = await api_client.post("/ingest/stream", json={
            "file_path": str(_PROBLEMATIC_CSV),
            "file_format": "csv",
        })
    assert r.status_code == 200
    assert r.headers.get("x-thread-id", "").startswith("ingest-acme-")
    assert "event: interrupt" in r.text


async def test_approve_404_when_no_checkpoint(api_client) -> None:
    failing = MagicMock()
    failing.ainvoke = AsyncMock(side_effect=ValueError("no checkpoint for thread"))
    with patch("api.routes.build_ingestion_graph", return_value=failing):
        r = await api_client.post("/ingest/missing-thread/approve", json={"approved": True})
    assert r.status_code == 404


async def test_approve_200_completed(api_client) -> None:
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={
        "normalized_items": [object(), object()],
        "flagged_items": [object()],
        "validation_errors": ["qualche avviso"],
        "error": None,
    })
    with patch("api.routes.build_ingestion_graph", return_value=graph):
        r = await api_client.post("/ingest/t-123/approve", json={"approved": True, "feedback": "ok"})
    assert r.status_code == 200
    body = r.json()
    assert body["thread_id"] == "t-123"
    assert body["status"] == "completed"
    assert body["total_items"] == 2
    assert body["flagged_count"] == 1


async def test_approve_200_rejected(api_client) -> None:
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={
        "normalized_items": [object()],
        "flagged_items": [object()],
        "validation_errors": [],
        "error": None,
    })
    with patch("api.routes.build_ingestion_graph", return_value=graph):
        r = await api_client.post("/ingest/t-999/approve", json={"approved": False})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"


async def test_ingest_stream_clean_emits_done(api_client, tmp_path) -> None:
    """Catalogo pulito → nessun interrupt → event: done (copre il ramo finale)."""
    f = tmp_path / "clean.csv"
    f.write_text("name,price\nServizio Valido,100\n", encoding="utf-8")
    clean = (
        '[{"name": "Servizio Valido", "description": "ok", "price": 100, '
        '"currency": "EUR", "confidence": 0.95}]'
    )
    with patch("agents.extractor._call_openai_compatible", new=AsyncMock(return_value=clean)), \
         patch("ingestion.graph._write_to_chroma_sync", MagicMock(return_value=1)):
        r = await api_client.post("/ingest/stream", json={
            "file_path": str(f), "file_format": "csv",
        })
    assert r.status_code == 200
    assert "event: done" in r.text
