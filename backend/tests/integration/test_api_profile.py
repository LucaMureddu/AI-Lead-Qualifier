"""
tests/integration/test_api_profile.py
--------------------------------------
GET/PUT /tenants/{id}/profile via ASGITransport.

V2.1: il profilo è memorizzato in Postgres (database/profiles.py) invece che
in file JSON su filesystem. I test mockano get_profile/upsert_profile per
evitare connessioni asyncpg reali.

get_profile/upsert_profile sono chiamate dirette nel corpo della route
(non Depends()), quindi patch() funziona normalmente per queste funzioni.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.config import get_settings

pytestmark = pytest.mark.integration


async def test_get_returns_defaults_when_unset(api_client) -> None:
    """Nessun profilo in DB → vengono restituiti i valori di default."""
    with patch("api.routes.get_profile", new=AsyncMock(return_value=None)):
        r = await api_client.get("/tenants/acme/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "acme"
    assert body["vat_rate"] == 0.22
    assert body["validity_days"] == 30


async def test_put_then_get_roundtrip(api_client) -> None:
    """PUT salva il profilo; GET lo rilegge correttamente."""
    payload = {"company_name": "ACME Srl", "vat_number": "IT123", "vat_rate": 0.1}

    with patch("api.routes.upsert_profile", new=AsyncMock(return_value=None)), \
         patch("api.routes.get_profile",
               new=AsyncMock(return_value={"company_name": "ACME Srl",
                                           "vat_number": "IT123",
                                           "vat_rate": 0.1})):
        put = await api_client.put("/tenants/acme/profile", json=payload)
        assert put.status_code == 200
        assert put.json()["company_name"] == "ACME Srl"

        got = await api_client.get("/tenants/acme/profile")
        assert got.status_code == 200
        assert got.json()["company_name"] == "ACME Srl"
        assert got.json()["tenant_id"] == "acme"


async def test_put_rejects_non_image_logo(api_client) -> None:
    """Logo non-immagine → 400 (validazione avviene prima dell'accesso al DB)."""
    r = await api_client.put(
        "/tenants/acme/profile",
        json={"logo_data_url": "data:text/plain;base64,QQ=="},
    )
    assert r.status_code == 400


async def test_put_rejects_oversized_profile(api_client, monkeypatch) -> None:
    """Profilo oltre limite → 413 (validazione avviene prima dell'accesso al DB)."""
    monkeypatch.setenv("PROFILE_MAX_BYTES", "50")
    get_settings.cache_clear()
    big_logo = "data:image/png;base64," + "A" * 1000
    r = await api_client.put("/tenants/acme/profile", json={"logo_data_url": big_logo})
    assert r.status_code == 413
