"""
adapters/factory.py
-------------------
Factory function that resolves the correct ``BaseDeliveryAdapter`` for a tenant.

Current behaviour
-----------------
Returns a ``ConsoleAdapter`` for all tenants — suitable for development and
the initial production rollout.

Scaling path
------------
Replace the body of ``get_delivery_adapter`` with a lookup against your
tenant configuration store (database, env map, secrets manager, etc.) to
return per-tenant adapters, e.g.:

    config = await fetch_tenant_config(tenant_id)        # your DB call
    if config.delivery_type == "webhook":
        return WebhookAdapter(url=config.webhook_url, tenant_id=tenant_id)
    return ConsoleAdapter()

The rest of the codebase (delivery_node, graph) never changes — only this
function grows.
"""

from __future__ import annotations

import logging

from adapters.base import BaseDeliveryAdapter
from adapters.console import ConsoleAdapter

logger: logging.Logger = logging.getLogger(__name__)


def get_delivery_adapter(tenant_id: str) -> BaseDeliveryAdapter:
    """
    Return the delivery adapter configured for ``tenant_id``.

    Parameters
    ----------
    tenant_id : str
        Tenant identifier.  Used for future per-tenant routing and for
        logging only in the current implementation.

    Returns
    -------
    BaseDeliveryAdapter
        The adapter instance to use for this tenant's delivery.
    """
    logger.debug("[factory] Resolving delivery adapter for tenant=%s", tenant_id)
    # TODO: replace with per-tenant lookup when webhook URLs are configured.
    return ConsoleAdapter()
