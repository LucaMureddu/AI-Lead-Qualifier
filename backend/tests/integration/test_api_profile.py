"""
tests/integration/test_api_profile.py
--------------------------------------
GET/PUT /tenants/{id}/profile via ASGITransport: default vuoti, upsert+rilettura,
logo non-immagine → 400, profilo oltre il limite → 413. Storage JSON in tmp
(PROFILES_DIR isolato dal conftest).
"""

from __future__ import annotations

import pytest

from core.config import get_settings

pytestmark = pytest.mark.integration


async def test_get_returns_defaults_when_unset(api_client) -> None:
    r = await api_client.get("/tenants/acme/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "acme"
    assert body["vat_rate"] == 0.22
    assert body["validity_days"] == 30


async def test_put_then_get_roundtrip(api_client) -> None:
    payload = {"company_name": "ACME Srl", "vat_number": "IT123", "vat_rate": 0.1}
    put = await api_client.put("/tenants/acme/profile", json=payload)
    assert put.status_code == 200
    assert put.json()["company_name"] == "ACME Srl"

    got = await api_client.get("/tenants/acme/profile")
    assert got.status_code == 200
    assert got.json()["company_name"] == "ACME Srl"
    assert got.json()["tenant_id"] == "acme"


async def test_put_rejects_non_image_logo(api_client) -> None:
    r = await api_client.put(
        "/tenants/acme/profile",
        json={"logo_data_url": "data:text/plain;base64,QQ=="},
    )
    assert r.status_code == 400


async def test_put_rejects_oversized_profile(api_client, monkeypatch) -> None:
    monkeypatch.setenv("PROFILE_MAX_BYTES", "50")
    get_settings.cache_clear()
    big_logo = "data:image/png;base64," + "A" * 1000
    r = await api_client.put("/tenants/acme/profile", json={"logo_data_url": big_logo})
    assert r.status_code == 413
