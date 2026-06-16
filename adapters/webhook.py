"""
adapters/webhook.py
-------------------
WebhookAdapter — production delivery adapter.

Sends the quote payload as a JSON POST to the tenant's configured webhook URL.

Design decisions
----------------
- A fresh ``httpx.AsyncClient`` is created per ``deliver()`` call.
  This avoids holding a persistent connection across retries (which may
  have a stale or half-closed TCP session) while still benefiting from
  httpx's built-in connection pooling within a single request lifecycle.
- Timeout is read from ``Settings.delivery_timeout_seconds`` so it can be
  tuned per deployment without code changes.
- HTTP error responses (4xx / 5xx) return ``False`` — delivery_node treats
  them as retryable failures.  Only status_code and lead_id are logged;
  the payload body is never written to logs to prevent PII leakage.
- Network-level exceptions (``httpx.RequestError``: connection refused,
  DNS failure, read/write timeout) are re-raised so delivery_node can catch
  and record them explicitly.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from adapters.base import BaseDeliveryAdapter
from core.config import get_settings

logger: logging.Logger = logging.getLogger(__name__)


class WebhookAdapter(BaseDeliveryAdapter):
    """
    Async HTTP delivery adapter.

    Parameters
    ----------
    url : str
        Webhook endpoint URL for the target tenant's CRM / integration layer.
    tenant_id : str
        Used in log messages only — never included in the HTTP request body
        beyond what the caller puts in ``payload``.
    """

    def __init__(self, url: str, tenant_id: str) -> None:
        self._url: str = url
        self._tenant_id: str = tenant_id

    async def deliver(self, payload: dict) -> bool:
        """
        POST ``payload`` as JSON to the configured webhook URL.

        Returns
        -------
        bool
            ``True``  — server responded with HTTP 2xx.
            ``False`` — server responded with HTTP 4xx / 5xx.

        Raises
        ------
        httpx.RequestError
            On network-level failures (timeout, DNS, connection refused).
            Callers (delivery_node) are responsible for catching this.
        """
        settings = get_settings()
        timeout: float = settings.delivery_timeout_seconds

        # lead_id is safe to log (it's a UUID, not PII).
        lead_id: Optional[str] = payload.get("lead_id", "unknown")

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response: httpx.Response = await client.post(
                    self._url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            except httpx.RequestError:
                # Re-raise: delivery_node distinguishes network errors from
                # HTTP-level failures and logs them with appropriate severity.
                logger.warning(
                    "[WebhookAdapter] Network error | tenant=%s | lead_id=%s | url=%s",
                    self._tenant_id,
                    lead_id,
                    self._url,
                )
                raise

        if response.is_success:
            logger.info(
                "[WebhookAdapter] Delivered | tenant=%s | lead_id=%s | status=%d",
                self._tenant_id,
                lead_id,
                response.status_code,
            )
            return True

        # HTTP error — log status code only, not body (may contain upstream PII).
        logger.warning(
            "[WebhookAdapter] HTTP error | tenant=%s | lead_id=%s | status=%d",
            self._tenant_id,
            lead_id,
            response.status_code,
        )
        return False
