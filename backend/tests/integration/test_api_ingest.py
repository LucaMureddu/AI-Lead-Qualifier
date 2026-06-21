"""
tests/integration/test_api_ingest.py
-------------------------------------
Endpoint /ingest/stream (header X-Thread-Id + event interrupt) e
/ingest/{thread_id}/approve (404 e 200) via ASGITransport.

V2.1 changes
------------
- IngestRequest usa object_key (S3 Object Key) invece di file_path (path locale).
- chunker_node scarica il file da S3 via services.storage.download_file().
  In test mocchiamo download_file per restituire bytes locali senza MinIO.
- /ingest/{id}/approve usa app.state.ingestion_graph (Depends); non esiste
  build_ingestion_graph in api.routes — i test erano scritti contro V1.

Nota sul mocking delle dipendenze FastAPI
-----------------------------------------
get_ingestion_graph è una funzione Depends() che legge app.state.ingestion_graph.
patch("api.routes.build_ingestion_graph") non funziona (la funzione non esiste
e Depends() cattura il riferimento originale a decorazione-tempo).
La tecnica corretta è sovrascrivere fastapi_app.state.ingestion_graph.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

_FLAGGED = '[{"name": "??", "price": 0, "currency": "EUR", "confidence": 0.3}]'

# Bytes CSV fittizi per il mock di download_file. Il contenuto non influisce
# sul risultato perché il LLM è mockato a restituire _FLAGGED indipendentemente
# dall'input del chunker.
_PROBLEMATIC_CSV_BYTES = b"name,price,description\nServizio Problematico,0,troppo corto\n"


# ── Stream tests (richiedono grafo reale + checkpointer) ──────────────────────

async def test_ingest_stream_header_and_interrupt(
    fastapi_app, api_client, checkpointer
) -> None:
    """Il file problematico forza un interrupt → SSE emette event: interrupt."""
    from ingestion.graph import build_ingestion_graph

    fastapi_app.state.ingestion_graph = build_ingestion_graph(checkpointer)

    with patch("agents.extractor._call_openai_compatible", new=AsyncMock(return_value=_FLAGGED)), \
         patch("services.storage.download_file",
               new=AsyncMock(return_value=_PROBLEMATIC_CSV_BYTES)):
        r = await api_client.post("/ingest/stream", json={
            "object_key": "acme/catalogo_problematico.csv",
            "file_format": "csv",
        })

    assert r.status_code == 200
    assert r.headers.get("x-thread-id", "").startswith("ingest-acme-")
    assert "event: interrupt" in r.text


async def test_ingest_stream_clean_emits_done(
    fastapi_app, api_client, checkpointer, tmp_path
) -> None:
    """Catalogo pulito → nessun interrupt → event: done (copre il ramo finale)."""
    from ingestion.graph import build_ingestion_graph

    fastapi_app.state.ingestion_graph = build_ingestion_graph(checkpointer)

    f = tmp_path / "clean.csv"
    f.write_text("name,price\nServizio Valido,100\n", encoding="utf-8")
    clean = (
        '[{"name": "Servizio Valido", "description": "ok", "price": 100, '
        '"currency": "EUR", "confidence": 0.95}]'
    )
    with patch("agents.extractor._call_openai_compatible", new=AsyncMock(return_value=clean)), \
         patch("ingestion.graph._write_to_pgvector", AsyncMock(return_value=1)), \
         patch("services.storage.download_file",
               new=AsyncMock(return_value=f.read_bytes())):
        r = await api_client.post("/ingest/stream", json={
            "object_key": "acme/clean.csv",
            "file_format": "csv",
        })

    assert r.status_code == 200
    assert "event: done" in r.text


# ── Approve tests (usano mock ingestion_graph) ────────────────────────────────

async def test_approve_404_when_no_checkpoint(fastapi_app, api_client) -> None:
    """Thread senza checkpoint → aget_state restituisce None → 404."""
    # Default: fastapi_app.state.ingestion_graph.aget_state = AsyncMock(return_value=None)
    r = await api_client.post("/ingest/missing-thread/approve", json={"approved": True})
    assert r.status_code == 404


async def test_approve_200_completed(fastapi_app, api_client) -> None:
    """approved:true + checkpoint valido → 200 completed con conteggi corretti."""
    snapshot = MagicMock()
    snapshot.values = {"tenant_id": "acme"}
    fastapi_app.state.ingestion_graph.aget_state = AsyncMock(return_value=snapshot)
    fastapi_app.state.ingestion_graph.ainvoke = AsyncMock(return_value={
        "normalized_items": [object(), object()],
        "flagged_items": [object()],
        "validation_errors": ["qualche avviso"],
        "error": None,
    })

    r = await api_client.post("/ingest/t-123/approve", json={"approved": True, "feedback": "ok"})

    assert r.status_code == 200
    body = r.json()
    assert body["thread_id"] == "t-123"
    assert body["status"] == "completed"
    assert body["total_items"] == 2
    assert body["flagged_count"] == 1


async def test_approve_200_rejected(fastapi_app, api_client) -> None:
    """approved:false → 200 rejected."""
    snapshot = MagicMock()
    snapshot.values = {"tenant_id": "acme"}
    fastapi_app.state.ingestion_graph.aget_state = AsyncMock(return_value=snapshot)
    fastapi_app.state.ingestion_graph.ainvoke = AsyncMock(return_value={
        "normalized_items": [object()],
        "flagged_items": [object()],
        "validation_errors": [],
        "error": None,
    })

    r = await api_client.post("/ingest/t-999/approve", json={"approved": False})

    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
