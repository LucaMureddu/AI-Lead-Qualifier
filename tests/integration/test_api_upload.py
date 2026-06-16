"""
tests/integration/test_api_upload.py
-------------------------------------
Endpoint /upload via ASGITransport: validazione estensione/dimensione/vuoto e
sanificazione del tenant_id (no path traversal). I file finiscono in tmp
(UPLOAD_DIR isolato dal conftest), niente scritture nel repo.
"""

from __future__ import annotations

import pytest

from core.config import get_settings

pytestmark = pytest.mark.integration


async def test_rejects_bad_extension(api_client) -> None:
    r = await api_client.post(
        "/upload",
        files={"file": ("catalog.txt", b"some,data", "text/plain")},
        data={"tenant_id": "acme"},
    )
    assert r.status_code == 400


async def test_rejects_empty_file(api_client) -> None:
    r = await api_client.post(
        "/upload",
        files={"file": ("catalog.csv", b"", "text/csv")},
        data={"tenant_id": "acme"},
    )
    assert r.status_code == 400


async def test_rejects_too_large(api_client, monkeypatch) -> None:
    monkeypatch.setenv("UPLOAD_MAX_BYTES", "10")
    get_settings.cache_clear()  # l'handler rilegge le settings a runtime
    r = await api_client.post(
        "/upload",
        files={"file": ("catalog.csv", b"x" * 50, "text/csv")},
        data={"tenant_id": "acme"},
    )
    assert r.status_code == 413


async def test_success_sanitizes_tenant_id(api_client) -> None:
    r = await api_client.post(
        "/upload",
        files={"file": ("catalog.csv", b"name,price\nA,1\n", "text/csv")},
        data={"tenant_id": "../evil"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["file_format"] == "csv"
    assert "evil" in body["file_path"]
    assert ".." not in body["file_path"]  # niente path traversal
