"""
tests/integration/test_api_lead_approve.py
-------------------------------------------
POST /lead/{thread_id}/approve — HITL per la qualificazione lead (V2/V3).

Flusso testato:
  1. Il job è in stato pending_review nel checkpointer Postgres.
  2. L'operatore invia approved:true → lo stato torna in coda (resumed) e
     run_qualification_task_resume viene accodato su ARQ.
  3. L'operatore invia approved:false → lo stato diventa error (rejected).
  4. Thread inesistente → 404.
  5. Job non in pending_review (es. completed) → 409.
  6. Tenant sbagliato → 403 (IDOR guard).

Nota sul mocking
----------------
get_graph/get_redis sono funzioni Depends() — patch() sul modulo non ha effetto
perché Depends cattura il riferimento originale alla decorazione della route.
La tecnica corretta è impostare fastapi_app.state.graph / fastapi_app.state.redis
prima della request (la fixture ``fastapi_app`` del conftest le pre-popola con
AsyncMock di default; i singoli test le sovrascrivono dove necessario).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.integration

_TENANT_ID = "acme"
_THREAD_ID = f"qualify-{_TENANT_ID}-lead-001"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_snapshot(status: str, tenant_id: str = _TENANT_ID) -> MagicMock:
    from core.state import LeadContext

    snapshot = MagicMock()
    snapshot.values = {
        "lead": LeadContext(
            lead_id="lead-001",
            tenant_id=tenant_id,
            raw_payload={"text": "Testo lead di test."},
        ),
        "status": status,
        "confidence_score": 0.60,
        "extracted_services": ["Cloud Migration"],
        "mapped_services": [],
        "total_quote": 0.0,
        "on_request_services": [],
        "error_detail": None,
    }
    return snapshot


# ═════════════════════════════════════════════════════════════════════════════
# POST /lead/{thread_id}/approve
# ═════════════════════════════════════════════════════════════════════════════

class TestApproveLead:
    @pytest.mark.asyncio
    async def test_approve_true_returns_resumed(self, fastapi_app, api_client) -> None:
        """approved:true → status='resumed', ARQ re-enqueue del task resume."""
        snapshot = _make_snapshot("pending_review")
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        fastapi_app.state.graph.aupdate_state = AsyncMock(return_value=None)

        r = await api_client.post(
            f"/lead/{_THREAD_ID}/approve",
            json={"approved": True, "feedback": "Ok, procedi."},
        )

        assert r.status_code == 200
        body = r.json()
        assert body["thread_id"] == _THREAD_ID
        assert body["status"] == "resumed"

    @pytest.mark.asyncio
    async def test_approve_true_enqueues_resume_task(self, fastapi_app, api_client) -> None:
        """approved:true deve accodare run_qualification_task_resume su ARQ."""
        snapshot = _make_snapshot("pending_review")
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        fastapi_app.state.graph.aupdate_state = AsyncMock(return_value=None)
        fastapi_app.state.redis.enqueue_job = AsyncMock(return_value=None)

        await api_client.post(f"/lead/{_THREAD_ID}/approve", json={"approved": True})

        fastapi_app.state.redis.enqueue_job.assert_awaited_once()
        call_args = fastapi_app.state.redis.enqueue_job.call_args
        assert call_args[0][0] == "run_qualification_task_resume"
        assert call_args[1]["thread_id"] == _THREAD_ID
        assert call_args[1]["tenant_id"] == _TENANT_ID

    @pytest.mark.asyncio
    async def test_approve_true_updates_state_with_max_confidence(self, fastapi_app, api_client) -> None:
        """approved:true deve impostare confidence_score=1.0 e status='queued'."""
        snapshot = _make_snapshot("pending_review")
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        fastapi_app.state.graph.aupdate_state = AsyncMock(return_value=None)

        await api_client.post(f"/lead/{_THREAD_ID}/approve", json={"approved": True})

        update_call = fastapi_app.state.graph.aupdate_state.call_args
        updated_fields: dict = update_call[0][1]
        assert updated_fields["human_approved"] is True
        assert updated_fields["confidence_score"] == 1.0
        assert updated_fields["status"] == "queued"

    @pytest.mark.asyncio
    async def test_reject_returns_rejected(self, fastapi_app, api_client) -> None:
        """approved:false → status='rejected', nessun re-enqueue ARQ."""
        snapshot = _make_snapshot("pending_review")
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        fastapi_app.state.graph.aupdate_state = AsyncMock(return_value=None)

        r = await api_client.post(
            f"/lead/{_THREAD_ID}/approve",
            json={"approved": False, "feedback": "Dati insufficienti."},
        )

        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "rejected"
        # Nessun task ARQ accodato dopo un rifiuto
        fastapi_app.state.redis.enqueue_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reject_updates_state_with_error(self, fastapi_app, api_client) -> None:
        """approved:false deve impostare human_approved=False e status='error'."""
        snapshot = _make_snapshot("pending_review")
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)
        fastapi_app.state.graph.aupdate_state = AsyncMock(return_value=None)

        await api_client.post(
            f"/lead/{_THREAD_ID}/approve",
            json={"approved": False, "feedback": "KO"},
        )

        update_call = fastapi_app.state.graph.aupdate_state.call_args
        updated_fields: dict = update_call[0][1]
        assert updated_fields["human_approved"] is False
        assert updated_fields["status"] == "error"
        assert updated_fields["review_feedback"] == "KO"

    @pytest.mark.asyncio
    async def test_approve_thread_not_found_returns_404(self, fastapi_app, api_client) -> None:
        """Thread inesistente (nessun checkpoint) → 404."""
        # Default: fastapi_app.state.graph.aget_state restituisce None → 404
        r = await api_client.post(
            "/lead/qualify-acme-nonexistent/approve",
            json={"approved": True},
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_approve_wrong_status_returns_409(self, fastapi_app, api_client) -> None:
        """Job già completato (status='completed') → 409 Conflict."""
        snapshot = _make_snapshot("completed")
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)

        r = await api_client.post(
            f"/lead/{_THREAD_ID}/approve",
            json={"approved": True},
        )
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_approve_wrong_tenant_returns_403(self, fastapi_app, api_client) -> None:
        """Tenant A non può approvare un job che appartiene a Tenant B (IDOR guard)."""
        # Il job appartiene a "altro_tenant", ma il JWT del client è per "acme"
        snapshot = _make_snapshot("pending_review", tenant_id="altro_tenant")
        fastapi_app.state.graph.aget_state = AsyncMock(return_value=snapshot)

        r = await api_client.post(
            "/lead/qualify-altro_tenant-lead-001/approve",
            json={"approved": True},
        )
        assert r.status_code == 403
