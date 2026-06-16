"""
adapters/
---------
Delivery adapter package.

Public surface
--------------
BaseDeliveryAdapter  — abstract interface all adapters must implement.
ConsoleAdapter       — dev/debug adapter, prints payload and returns True.
WebhookAdapter       — production adapter, POSTs payload to a tenant webhook URL.
get_delivery_adapter — factory that resolves the correct adapter per tenant.
"""

from adapters.base import BaseDeliveryAdapter
from adapters.console import ConsoleAdapter
from adapters.factory import get_delivery_adapter
from adapters.webhook import WebhookAdapter

__all__ = [
    "BaseDeliveryAdapter",
    "ConsoleAdapter",
    "WebhookAdapter",
    "get_delivery_adapter",
]
