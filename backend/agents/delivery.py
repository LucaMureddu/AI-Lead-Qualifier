"""
agents/delivery.py
------------------
DeliveryNode — forwards the calculated quote to the tenant's external system.

V2 changes vs V1
----------------
- State type: LeadState → AgentState
- Reads lead_id/tenant_id from state["lead"].*
- Removed: sse_logs; uses structlog instead
- Sets status="completed" on successful delivery
"""

from __future__ import annotations

from typing import Any, Dict

import httpx
import structlog

from adapters.factory import get_delivery_adapter
from core.state import AgentState

log = structlog.get_logger()


async def delivery_node(state: AgentState) -> Dict[str, Any]:
    """
    LangGraph node: deliver the lead quote to the tenant's external system.

    Reads: state["lead"], state["total_quote"], state["mapped_services"]
    Writes: delivery_status, delivery_attempts, delivery_error, status
    """
    lead_id: str = state["lead"].lead_id
    tenant_id: str = state["lead"].tenant_id
    attempts: int = state.get("delivery_attempts", 0) + 1

    log.info(
        "delivery.attempt",
        lead_id=lead_id,
        tenant_id=tenant_id,
        attempt=attempts,
    )

    adapter = get_delivery_adapter(tenant_id)
    on_request: list = state.get("on_request_services", [])
    payload: Dict[str, Any] = {
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "total_quote": state.get("total_quote", 0.0),
        "total_is_partial": len(on_request) > 0,
        "mapped_services": state.get("mapped_services", []),
        "on_request_services": on_request,
    }

    try:
        success: bool = await adapter.deliver(payload)
    except httpx.RequestError as exc:
        log.warning(
            "delivery.network_error",
            lead_id=lead_id,
            tenant_id=tenant_id,
            attempt=attempts,
            error=str(exc),
        )
        return {
            "delivery_status": "FAILED",
            "delivery_attempts": attempts,
            "delivery_error": f"Network error attempt {attempts}: {type(exc).__name__}: {exc}",
        }
    except Exception as exc:
        log.exception(
            "delivery.unexpected_error",
            lead_id=lead_id,
            tenant_id=tenant_id,
            attempt=attempts,
        )
        return {
            "delivery_status": "FAILED",
            "delivery_attempts": attempts,
            "delivery_error": f"Unexpected error attempt {attempts}: {type(exc).__name__}: {exc}",
        }

    if success:
        log.info("delivery.success", lead_id=lead_id, tenant_id=tenant_id, attempt=attempts)
        return {
            "delivery_status": "SUCCESS",
            "delivery_attempts": attempts,
            "delivery_error": None,
            "status": "completed",
        }

    log.warning("delivery.failed", lead_id=lead_id, tenant_id=tenant_id, attempt=attempts)
    return {
        "delivery_status": "FAILED",
        "delivery_attempts": attempts,
        "delivery_error": f"Adapter returned False on attempt {attempts}",
    }
