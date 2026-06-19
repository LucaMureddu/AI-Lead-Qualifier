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


def _format_quote_body(
    mapped_services: list[dict],
    total_quote: float,
    total_is_partial: bool,
) -> str:
    """
    Build a human-readable quote body for the delivery email.

    Three-branch logic per service line:
    - is_on_request → "• {nome} — su richiesta"
    - price == 0.0  → "• {nome} — Gratis"
    - else          → "• {nome} — {price:.2f} €"

    This correctly distinguishes a legitimately free service (price=0.0,
    is_on_request=False) from one whose price is unknown (is_on_request=True,
    price stored as 0.0 in the DB).
    """
    lines: list[str] = ["Riepilogo servizi:", ""]
    for svc in mapped_services:
        nome: str = svc.get("matched_name") or svc.get("service", "?")
        price: float = float(svc.get("price", 0.0))
        if svc.get("is_on_request", False):
            lines.append(f"• {nome} — su richiesta")
        elif price == 0.0:
            lines.append(f"• {nome} — Gratis")
        else:
            lines.append(f"• {nome} — {price:.2f} €")

    lines.append("")
    if total_is_partial:
        lines.append(
            f"Totale parziale: {total_quote:.2f} € "
            f"(alcuni servizi sono da preventivare)"
        )
    else:
        lines.append(f"Totale: {total_quote:.2f} €")

    return "\n".join(lines)


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
    mapped: list = state.get("mapped_services", [])
    total_quote: float = state.get("total_quote", 0.0)
    total_is_partial: bool = len(on_request) > 0
    payload: Dict[str, Any] = {
        "lead_id": lead_id,
        "tenant_id": tenant_id,
        "total_quote": total_quote,
        "total_is_partial": total_is_partial,
        "mapped_services": mapped,
        "on_request_services": on_request,
        "quote_body": _format_quote_body(mapped, total_quote, total_is_partial),
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
