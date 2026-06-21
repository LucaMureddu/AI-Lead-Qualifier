"""
tests/integration/test_api_admin.py
-------------------------------------
DELETE /api/v1/tenants/{tenant_id}/vector-data — reset hard pgvector.

Casi coperti:
  1. Happy path: il tenant cancella i propri dati vettoriali → 200.
  2. confirm_wipe=false → 400 (guard esplicito richiesto).
  3. IDOR fix: tenant A non può cancellare i dati di tenant B → 403.
  4. Errore DB durante il wipe → 500.

Nota: httpx.AsyncClient.delete() non accetta json= nelle versioni recenti.
Si usa client.request("DELETE", url, json=...) per passare un corpo JSON.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.integration

_TENANT_ID = "acme"


# ═════════════════════════════════════════════════════════════════════════════
# DELETE /api/v1/tenants/{tenant_id}/vector-data
# ═════════════════════════════════════════════════════════════════════════════

class TestWipeTenantVectorData:
    @pytest.mark.asyncio
    async def test_wipe_own_data_returns_200(self, api_client) -> None:
        """Happy path: il tenant autentico cancella i propri dati vettoriali."""
        with patch("api.routes.wipe_tenant", new=AsyncMock(return_value=42)):
            r = await api_client.request(
                "DELETE",
                f"/api/v1/tenants/{_TENANT_ID}/vector-data",
                json={"confirm_wipe": True},
            )

        assert r.status_code == 200
        body = r.json()
        assert body["tenant_id"] == _TENANT_ID
        assert body["rows_deleted"] == 42

    @pytest.mark.asyncio
    async def test_wipe_zero_rows_returns_200(self, api_client) -> None:
        """Anche se il catalogo è già vuoto, il wipe restituisce 200."""
        with patch("api.routes.wipe_tenant", new=AsyncMock(return_value=0)):
            r = await api_client.request(
                "DELETE",
                f"/api/v1/tenants/{_TENANT_ID}/vector-data",
                json={"confirm_wipe": True},
            )

        assert r.status_code == 200
        assert r.json()["rows_deleted"] == 0

    @pytest.mark.asyncio
    async def test_wipe_without_confirm_returns_400(self, api_client) -> None:
        """confirm_wipe=false → 400: l'operazione non viene eseguita."""
        with patch("api.routes.wipe_tenant", new=AsyncMock(return_value=0)) as mock_wipe:
            r = await api_client.request(
                "DELETE",
                f"/api/v1/tenants/{_TENANT_ID}/vector-data",
                json={"confirm_wipe": False},
            )

        assert r.status_code == 400
        # Il DB non deve essere toccato
        mock_wipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idor_tenant_a_cannot_wipe_tenant_b(self, api_client) -> None:
        """
        Fix IDOR (V2.1): il JWT di "acme" non può cancellare i dati di "altro_tenant".
        """
        with patch("api.routes.wipe_tenant", new=AsyncMock(return_value=0)) as mock_wipe:
            r = await api_client.request(
                "DELETE",
                "/api/v1/tenants/altro_tenant/vector-data",
                json={"confirm_wipe": True},
            )

        assert r.status_code == 403
        assert "negato" in r.json()["detail"].lower()
        mock_wipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idor_cannot_wipe_with_path_injection(self, api_client) -> None:
        """
        Verifica che il confronto sia su tenant_id esatto — non basta che
        il path contenga il proprio tenant_id come prefisso.
        """
        with patch("api.routes.wipe_tenant", new=AsyncMock(return_value=0)) as mock_wipe:
            r = await api_client.request(
                "DELETE",
                "/api/v1/tenants/acme_evil/vector-data",
                json={"confirm_wipe": True},
            )

        assert r.status_code == 403
        mock_wipe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wipe_db_error_returns_500(self, api_client) -> None:
        """Errore imprevisto durante il wipe pgvector → 500."""
        with patch(
            "api.routes.wipe_tenant",
            new=AsyncMock(side_effect=RuntimeError("pgvector boom")),
        ):
            r = await api_client.request(
                "DELETE",
                f"/api/v1/tenants/{_TENANT_ID}/vector-data",
                json={"confirm_wipe": True},
            )

        assert r.status_code == 500

    @pytest.mark.asyncio
    async def test_wipe_unauthenticated_returns_401(self) -> None:
        """Senza JWT → 401, non si arriva mai al DB."""
        from main import create_app
        from httpx import ASGITransport, AsyncClient

        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            with patch("api.routes.wipe_tenant", new=AsyncMock(return_value=0)) as mock_wipe:
                r = await client.request(
                    "DELETE",
                    f"/api/v1/tenants/{_TENANT_ID}/vector-data",
                    json={"confirm_wipe": True},
                )

        assert r.status_code == 401
        mock_wipe.assert_not_awaited()
