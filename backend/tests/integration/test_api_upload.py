"""
tests/integration/test_api_upload.py
-------------------------------------
Endpoint /upload via ASGITransport: validazione estensione/dimensione/vuoto e
sanificazione del tenant_id (no path traversal).

V2.1: l'endpoint scrive su S3 (MinIO) invece che su filesystem locale.
- UploadResponse restituisce ``object_key`` (S3 Object Key) invece di ``file_path``.
- I test mockano api.routes.upload_file per evitare connessioni S3 reali.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.config import get_settings

pytestmark = pytest.mark.integration


async def test_rejects_bad_extension(api_client) -> None:
    r = await api_client.post(
        "/upload",
        files={"file": ("catalog.txt", b"some,data", "text/plain")},
    )
    assert r.status_code == 400


async def test_rejects_empty_file(api_client) -> None:
    r = await api_client.post(
        "/upload",
        files={"file": ("catalog.csv", b"", "text/csv")},
    )
    assert r.status_code == 400


async def test_rejects_too_large(api_client, monkeypatch) -> None:
    monkeypatch.setenv("UPLOAD_MAX_BYTES", "10")
    get_settings.cache_clear()
    r = await api_client.post(
        "/upload",
        files={"file": ("catalog.csv", b"x" * 50, "text/csv")},
    )
    assert r.status_code == 413


async def test_success_sanitizes_tenant_id(api_client) -> None:
    """Il tenant_id viene dal JWT (claim 'sub' = 'acme') — non dal form.

    V2.1: upload_file è mockato → nessuna connessione S3.
    La risposta usa object_key (non file_path).
    """
    fake_key = "acme/abc123.csv"
    with patch("api.routes.upload_file", new=AsyncMock(return_value=fake_key)):
        r = await api_client.post(
            "/upload",
            files={"file": ("catalog.csv", b"name,price\nA,1\n", "text/csv")},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["file_format"] == "csv"
    # V2.1: la risposta usa object_key, non file_path.
    assert "acme" in body["object_key"]
