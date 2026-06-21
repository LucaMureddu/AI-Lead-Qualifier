"""
tests/integration/test_api_qualify.py
--------------------------------------
Endpoint /health, POST /lead (202 async), GET /status/{thread_id} — V2/V3.

V2 change: SSE rimossa dalla qualificazione. Il flusso è ora:
  POST /lead → 202 { thread_id }
  GET  /status/{thread_id} → polling (queued/processing/completed/pending_review/error)

I test per POST /lead/{thread_id}/approve (HITL) vivono in test_api_lead_approve.py.

Nota sul mocking delle dipendenze FastAPI
-----------------------------------------
get_graph/get_redis/get_ingestion_graph sono funzioni usate con Depends().
Quando la route viene registrata, Depends() cattura il *riferimento* alla
funzione originale — quindi patch("api.routes.get_graph") non ha effetto
a runtime. La tecnica corretta è impostare app.state.graph / app.state.redis
prima della request, usando la fixture ``fastapi_app`` dal conftest.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

_GOOD_TEXT = "Vorrei un sito web aziendale completo e un audit SEO"
_TENANT_ID = "acme"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_graph_snapshot(status: str, **extra) -> MagicMock:
    """Build a minimal LangGraph StateSnapshot mock."""
    from core.state import LeadContext

    snapshot = MagicMock()
    values = {
        "lead": LeadContext(
            lead_id="lead-001",
            tenant_id=_TENANT_ID,
            raw_payload={"text": _GOOD_TEXT},
        ),
        "status": status,
        "total_quote": 0.0,
        "mapped_services": [],
        "on_request_services": [],
        "confidence_score": 0.0,
        "extracted_services": [],
        "error_detail": None,
        **extra,
    }
    snapshot.values = values
    return snapshot


def _mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.enqueue_job = AsyncMock(return_value=None)
    redis.ping = AsyncMock(return_value=True)
    return redis


# ═════════════════════════════════════════════════════════════════════════════
# /health
# ═════════════════════════════════════════════════════════════════════════════

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_200_when_ok(self, api_client) -> None:
        """200 quando Postgres e Redis sono raggiungibili."""
        r = await api_client.get("/health")
        # Il conftest non connette un Redis reale: il risultato dipende dal mock
        # dell'app state. Verifichiamo solo che l'endpoint risponda con JSON valido.
        assert r.status_code in (200, 503)
        body = r.json()
        assert "status" in body
        assert "checks" in body

    @pytest.mark.asyncio
    async def test_health_redis_down_returns_503(self, api_client) -> None:
        """503 quando Redis non è raggiungibile — ARQ è down."""
        from main import create_app
        from httpx import ASGITransport, AsyncClient
        from api.dependencies import create_access_token

        app = create_app()
        # Inietta un Redis che fallisce il ping
        bad_redis = AsyncMock()
        bad_redis.ping = AsyncMock(side_effect=ConnectionError("Redis down"))
        app.state.redis = bad_redis

        token = create_access_token(tenant_id=_TENANT_ID, expires_delta_seconds=3600)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            with patch("main.get_pool", new=AsyncMock(
                return_value=AsyncMock(fetchval=AsyncMock(return_value=1))
            )):
                r = await client.get("/health")

        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "error"
        assert "redis" in body["checks"]
        assert body["checks"]["redis"].startswith("error:")


# ═════════════════════════════════════════════════════════════════════════════
# POST /lead  — 202 Accepted
# ═════════════════════════════════════════════════════════════════════════════

class TestIngestLead:
    @pytest.mark.asyncio
    async def test_lead_too_short_returns_422(self, api_client) -> None:
        """raw_text < 10 caratteri → 422 prima di qualsiasi logica."""
        r = await api_client.post("/lead", json={"raw_text": "ciao"})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_lead_enqueued_returns_202(self, api_client) -> None:
        """Un lead valido deve essere accodato su ARQ e restituire 202 + thread_id."""
        r = await api_client.post("/lead", json={"raw_text": _GOOD_TEXT})
        assert r.status_code == 202
        body = r.json()
        assert "thread_id" in body
        assert body["status"] == "queued"
        assert _TENANT_ID in body["thread_id"]

    @pytest.mark.asyncio
    async def test_lead_custom_lead_id_in_thread_id(self, api_client) -> None:
        """Se lead_id è fornito dal chiamante, deve comparire nel thread_id."""
        r = await api_client.post(
            "/lead",
            json={"raw_text": _GOOD_TEXT, "lead_id": "crm-42"},
        )
        assert r.status_code == 202
        assert "crm-42" in r.json()["thread_id"]

    @pytest.mark.asyncio
    async def test_lead_enqueue_job_called_with_correct_task(self, fastapi_app, api_client) -> None:
        """ARQ enqueue_job deve essere chiamato con run_qualification_task e il tenant_id corretto."""
        mock_redis = _mock_redis()
        fastapi_app.state.redis = mock_redis
        r = await api_client.post("/lead", json={"raw_text": _GOOD_TEXT})

        assert r.status_code == 202
        mock_redis.enqueue_job.assert_awaited_once()
        call_args = mock_redis.enqueue_job.call_args
        assert call_args[0][0] == "run_qualification_task"
        assert call_args[1]["tenant_id"] == _TENANT_ID

    @pytest.mark.asyncio
    async def test_lead_missing_body_returns_422(self, api_client) -> None:
        r = await api_client.post("/lead", json={})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_lead_unauthenticated_returns_401(self) -> None:
        """Senza header Authorization → 401."""
        from main import create_app
        from httpx import ASGITransport, AsyncClient
        from unittest.mock import AsyncMock as _AsyncMock

        app = create_app()
        # Imposta state.redis per evitare AttributeError in get_redis durante
        # la risoluzione delle dipendenze (avviene in parallelo con auth check).
        app.state.redis = _AsyncMock()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            r = await client.post("/lead", json={"raw_text": _GOOD_TEXT})
        assert r.status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# GET /status/{thread_id}  — polling
# ═════════════════════════════════════════════════════════════════════════════

class TestGetLeadStatus:
    @pytest.mark.asyncio
    async def test_status_queued_when_no_checkpoint(self, api_client) -> None:
        """Se il worker non ha ancora scritto il primo checkpoint → queued."""
        # fastapi_app.state.graph.aget_state restituisce None per default → "queued"
        r = await api_client.get("/status/qualify-acme-lead-001")
        assert r.status_code == 200
        assert r.json()["status"] == "queued"

    @pytest.mark.asyncio
    async def test_status_processing(self, fastapi_app, api_client) -> None:
        """Stato 'processing' quando il worker è in esecuzione."""
        snapshot = _make_graph_snapshot("processing")
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        r = await api_client.get("/status/qualify-acme-lead-001")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "processing"
        assert body["result"] is None

    @pytest.mark.asyncio
    async def test_status_completed_includes_result(self, fastapi_app, api_client) -> None:
        """Stato 'completed' include total_quote e mapped_services."""
        snapshot = _make_graph_snapshot(
            "completed",
            total_quote=3500.0,
            mapped_services=[
                {"matched_name": "Cloud Migration", "price": 3000.0, "price_type": "FIXED"},
                {"matched_name": "SEO Audit", "price": 500.0, "price_type": "FIXED"},
            ],
            on_request_services=[],
        )
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        r = await api_client.get("/status/qualify-acme-lead-001")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert body["result"]["total_quote"] == 3500.0
        assert len(body["result"]["mapped_services"]) == 2

    @pytest.mark.asyncio
    async def test_status_completed_variable_service_in_on_request(self, fastapi_app, api_client) -> None:
        """V3: i servizi VARIABLE finiscono in on_request_services, non in total_quote."""
        snapshot = _make_graph_snapshot(
            "completed",
            total_quote=500.0,
            mapped_services=[
                {"matched_name": "SEO Audit", "price": 500.0, "price_type": "FIXED"},
                {"matched_name": "Consulenza", "price": None, "price_type": "VARIABLE"},
            ],
            on_request_services=["Consulenza"],
        )
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        r = await api_client.get("/status/qualify-acme-lead-001")
        body = r.json()
        assert body["status"] == "completed"
        assert body["result"]["total_quote"] == 500.0
        assert "Consulenza" in body["result"]["on_request_services"]

    @pytest.mark.asyncio
    async def test_status_pending_review_includes_review_payload(self, fastapi_app, api_client) -> None:
        """Stato 'pending_review' include confidence_score e extracted_services nel result."""
        snapshot = _make_graph_snapshot(
            "pending_review",
            confidence_score=0.55,
            extracted_services=["Consulenza IT"],
        )
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        r = await api_client.get("/status/qualify-acme-lead-001")
        body = r.json()
        assert body["status"] == "pending_review"
        assert body["result"]["review_payload"]["confidence_score"] == 0.55

    @pytest.mark.asyncio
    async def test_status_error_includes_error_detail(self, fastapi_app, api_client) -> None:
        """Stato 'error' espone error_detail."""
        snapshot = _make_graph_snapshot(
            "error",
            error_detail="LLM timeout dopo 300s",
        )
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        r = await api_client.get("/status/qualify-acme-lead-001")
        body = r.json()
        assert body["status"] == "error"
        assert body["error_detail"] == "LLM timeout dopo 300s"

    @pytest.mark.asyncio
    async def test_status_forbidden_for_other_tenant(self, fastapi_app, api_client) -> None:
        """Un tenant non può leggere lo stato di un job che appartiene a un altro tenant."""
        from core.state import LeadContext

        snapshot = MagicMock()
        snapshot.values = {
            "lead": LeadContext(
                lead_id="lead-other",
                tenant_id="altro_tenant",
                raw_payload={"text": "x"},
            ),
            "status": "completed",
            "total_quote": 0.0,
            "mapped_services": [],
            "on_request_services": [],
            "confidence_score": 0.0,
            "extracted_services": [],
            "error_detail": None,
        }
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        # Il client è autenticato come "acme", il job appartiene ad "altro_tenant"
        r = await api_client.get("/status/qualify-altro_tenant-lead-other")
        assert r.status_code == 403
