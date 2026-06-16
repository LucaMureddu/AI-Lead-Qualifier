"""
tests/test_webhook_adapter.py
------------------------------
Isolated unit tests for WebhookAdapter.

Uses ``httpx.MockTransport`` (built into httpx, no extra deps) to intercept
outbound HTTP calls without touching the network.

Run with:
    pytest tests/test_webhook_adapter.py -v
"""

from __future__ import annotations

import pytest
import httpx
import pytest_asyncio

from adapters.webhook import WebhookAdapter

TENANT_ID = "test_tenant"
WEBHOOK_URL = "https://crm.example.com/webhooks/lead"

SAMPLE_PAYLOAD = {
    "lead_id": "abc-123",
    "tenant_id": TENANT_ID,
    "total_quote": 1500.0,
    "mapped_services": [{"service": "Consulenza", "price": 1500.0}],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_adapter(handler) -> WebhookAdapter:
    """
    Return a WebhookAdapter whose httpx.AsyncClient uses a MockTransport.

    ``handler`` is a callable(request) -> httpx.Response injected via
    monkeypatching AsyncClient.__aenter__ so the adapter code is unchanged.
    """
    # We patch AsyncClient at the module level so the adapter's internal
    # `async with httpx.AsyncClient(...) as client:` picks up the mock.
    return WebhookAdapter(url=WEBHOOK_URL, tenant_id=TENANT_ID)


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deliver_success(monkeypatch):
    """Adapter returns True when the server responds with HTTP 200."""

    async def mock_post(self, url, **kwargs):
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    adapter = WebhookAdapter(url=WEBHOOK_URL, tenant_id=TENANT_ID)
    result = await adapter.deliver(SAMPLE_PAYLOAD)

    assert result is True


@pytest.mark.asyncio
async def test_deliver_http_error_returns_false(monkeypatch):
    """Adapter returns False (not raises) on HTTP 500 — retryable by delivery_node."""

    async def mock_post(self, url, **kwargs):
        return httpx.Response(500, text="Internal Server Error")

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    adapter = WebhookAdapter(url=WEBHOOK_URL, tenant_id=TENANT_ID)
    result = await adapter.deliver(SAMPLE_PAYLOAD)

    assert result is False


@pytest.mark.asyncio
async def test_deliver_http_404_returns_false(monkeypatch):
    """Adapter returns False on HTTP 404 (misconfigured URL, not a crash)."""

    async def mock_post(self, url, **kwargs):
        return httpx.Response(404, text="Not Found")

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    adapter = WebhookAdapter(url=WEBHOOK_URL, tenant_id=TENANT_ID)
    result = await adapter.deliver(SAMPLE_PAYLOAD)

    assert result is False


@pytest.mark.asyncio
async def test_deliver_timeout_raises_request_error(monkeypatch):
    """
    Adapter re-raises httpx.RequestError on timeout so delivery_node can
    catch it and record it as a FAILED attempt.
    """

    async def mock_post(self, url, **kwargs):
        raise httpx.ReadTimeout("timed out", request=None)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    adapter = WebhookAdapter(url=WEBHOOK_URL, tenant_id=TENANT_ID)

    with pytest.raises(httpx.RequestError):
        await adapter.deliver(SAMPLE_PAYLOAD)


@pytest.mark.asyncio
async def test_deliver_connection_refused_raises_request_error(monkeypatch):
    """Network-level errors also surface as httpx.RequestError."""

    async def mock_post(self, url, **kwargs):
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    adapter = WebhookAdapter(url=WEBHOOK_URL, tenant_id=TENANT_ID)

    with pytest.raises(httpx.RequestError):
        await adapter.deliver(SAMPLE_PAYLOAD)
