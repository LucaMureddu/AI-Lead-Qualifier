"""
tests/unit/test_adapters.py
---------------------------
ConsoleAdapter (sempre True, nessuna rete) e factory get_delivery_adapter.
WebhookAdapter è coperto a parte in tests/test_webhook_adapter.py.
"""

from __future__ import annotations

import pytest

from adapters.base import BaseDeliveryAdapter
from adapters.console import ConsoleAdapter
from adapters.factory import get_delivery_adapter

pytestmark = pytest.mark.unit


async def test_console_adapter_returns_true() -> None:
    result = await ConsoleAdapter().deliver({"lead_id": "x", "total_quote": 1.0})
    assert result is True


def test_factory_returns_console_adapter() -> None:
    adapter = get_delivery_adapter("acme")
    assert isinstance(adapter, ConsoleAdapter)
    assert isinstance(adapter, BaseDeliveryAdapter)
