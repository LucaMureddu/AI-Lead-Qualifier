"""
agents/delivery.py
------------------
DeliveryNode — forwards the calculated quote to the tenant's external system.

Responsibilities
----------------
1. Resolve the correct adapter for the tenant via ``get_delivery_adapter``.
2. Build a PII-safe payload from the current graph state.
3. Attempt delivery; update ``delivery_status``, ``delivery_attempts``,
   and ``delivery_error`` in the returned state patch.

What this node does NOT do
--------------------------
- It never makes HTTP calls directly (SRP: that belongs to the adapter).
- It never reads ``settings.delivery_max_attempts`` — that is the concern
  of the conditional router ``route_after_delivery`` in ``core/graph.py``.
- It never logs payload contents — only lead_id, tenant_id, and attempt count.

Retry contract
--------------
``delivery_attempts`` is incremented at the TOP of every invocation so the
router always sees the correct count when deciding whether to retry.  The
router (not this node) enforces the ceiling.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from adapters.factory import get_delivery_adapter
from core.state import LeadState

logger: logging.Logger = logging.getLogger(__name__)


async def delivery_node(state: LeadState) -> Dict[str, Any]:
    """
    LangGraph node: deliver the lead quote to the tenant's external system.

    Parameters
    ----------
    state : LeadState
        Current graph state.  Reads ``lead_info``, ``total_quote``,
        ``mapped_services``, and ``delivery_attempts``.

    Returns
    -------
    dict
        State patch with updated ``delivery_status``, ``delivery_attempts``,
        ``delivery_error``, and a new ``sse_logs`` entry.
    """
    lead_id: str = state["lead_info"].id
    tenant_id: str = state["lead_info"].tenant_id
    attempts: int = state.get("delivery_attempts", 0) + 1

    logger.info(
        "[delivery] Attempt %d | tenant=%s | lead_id=%s",
        attempts,
        tenant_id,
        lead_id,
    )

    adapter = get_delivery_adapter(tenant_id)

    # PII-safe payload: only processed/derived fields, never raw_text.
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
        # Network-level failure (timeout, DNS, connection refused).
        # Safe to log exception type and lead/tenant identifiers — no payload.
        error_msg: str = (
            f"Network error on attempt {attempts} | "
            f"tenant={tenant_id} | lead_id={lead_id} | {type(exc).__name__}: {exc}"
        )
        logger.warning("[delivery] %s", error_msg)
        sse_entry: str = (
            f"[DELIVERY] FAILED (network) | tenant={tenant_id} | "
            f"lead_id={lead_id} | attempt={attempts}"
        )
        return {
            "delivery_status": "FAILED",
            "delivery_attempts": attempts,
            "delivery_error": error_msg,
            "sse_logs": [sse_entry],
        }

    except Exception as exc:  # noqa: BLE001
        # Unexpected adapter-internal error — surface it without payload details.
        error_msg = (
            f"Unexpected error on attempt {attempts} | "
            f"tenant={tenant_id} | lead_id={lead_id} | {type(exc).__name__}: {exc}"
        )
        logger.exception("[delivery] %s", error_msg)
        sse_entry = (
            f"[DELIVERY] FAILED (unexpected) | tenant={tenant_id} | "
            f"lead_id={lead_id} | attempt={attempts}"
        )
        return {
            "delivery_status": "FAILED",
            "delivery_attempts": attempts,
            "delivery_error": error_msg,
            "sse_logs": [sse_entry],
        }

    if success:
        sse_entry = (
            f"[DELIVERY] SUCCESS | tenant={tenant_id} | "
            f"lead_id={lead_id} | attempt={attempts}"
        )
        logger.info("[delivery] %s", sse_entry)
        return {
            "delivery_status": "SUCCESS",
            "delivery_attempts": attempts,
            "delivery_error": None,
            "sse_logs": [sse_entry],
        }

    # Adapter returned False (HTTP error, upstream rejection, etc.)
    error_msg = (
        f"Adapter returned False on attempt {attempts} | "
        f"tenant={tenant_id} | lead_id={lead_id}"
    )
    logger.warning("[delivery] %s", error_msg)
    sse_entry = (
        f"[DELIVERY] FAILED (adapter) | tenant={tenant_id} | "
        f"lead_id={lead_id} | attempt={attempts}"
    )
    return {
        "delivery_status": "FAILED",
        "delivery_attempts": attempts,
        "delivery_error": error_msg,
        "sse_logs": [sse_entry],
    }
