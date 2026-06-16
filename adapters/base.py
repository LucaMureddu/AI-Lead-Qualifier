"""
adapters/base.py
----------------
Abstract base class that every delivery adapter must implement.

Contract
--------
- ``deliver(payload)`` must be a coroutine (async) so it can be awaited
  directly inside the async LangGraph delivery_node without blocking.
- It must return ``True`` on confirmed delivery and ``False`` on any
  recoverable failure (HTTP error, adapter-side validation, etc.).
- Unrecoverable / network-level exceptions (e.g. ``httpx.RequestError``)
  may be raised and will be caught by delivery_node.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseDeliveryAdapter(ABC):
    """Interface for all delivery adapters."""

    @abstractmethod
    async def deliver(self, payload: dict) -> bool:
        """
        Attempt to deliver ``payload`` to the external system.

        Parameters
        ----------
        payload : dict
            Structured quote data for the lead.  Must never contain raw PII;
            callers are responsible for sanitising before calling this method.

        Returns
        -------
        bool
            ``True``  — delivery confirmed (e.g. HTTP 2xx, message enqueued).
            ``False`` — delivery failed but the error is recoverable
                        (e.g. HTTP 4xx/5xx, adapter validation error).

        Raises
        ------
        httpx.RequestError
            Propagated from ``WebhookAdapter`` for network-level failures
            (connection refused, timeout, DNS error).  delivery_node catches
            this and treats it as a retryable failure.
        """
        ...
