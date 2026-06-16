"""
adapters/console.py
-------------------
ConsoleAdapter — development / smoke-test delivery adapter.

Prints the payload to stdout via the standard logger and unconditionally
returns True.  Use this in local dev and unit tests to avoid hitting real
HTTP endpoints.

Security note: this adapter logs the full payload.  Never configure it in
production environments where payloads may contain sensitive business data.
"""

from __future__ import annotations

import json
import logging

from adapters.base import BaseDeliveryAdapter

logger: logging.Logger = logging.getLogger(__name__)


class ConsoleAdapter(BaseDeliveryAdapter):
    """Delivery adapter that writes the payload to the application log."""

    async def deliver(self, payload: dict) -> bool:
        """Log payload as formatted JSON and return True unconditionally."""
        logger.info(
            "[ConsoleAdapter] Delivering payload:\n%s",
            json.dumps(payload, indent=2, default=str),
        )
        return True
