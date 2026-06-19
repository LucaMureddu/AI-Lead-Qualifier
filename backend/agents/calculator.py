"""
agents/calculator.py
--------------------
CalculatorNode — pure Python, zero LLM.

V2 changes vs V1
----------------
- State type: LeadState → AgentState
- Reads lead_id from state["lead"].lead_id
- Removed: sse_logs; uses structlog instead
- Sets status="completed" on success
"""

from __future__ import annotations

from typing import Dict, List

import structlog

from core.state import AgentState

log = structlog.get_logger()


def _sum_prices(mapped_services: List[Dict]) -> float:
    total: float = 0.0
    for entry in mapped_services:
        total += float(entry["price"])
    return round(total, 2)


def calculator_node(state: AgentState) -> Dict:
    """
    LangGraph node: compute the total quote from mapped_services.

    Reads: state["lead"].lead_id, state["mapped_services"]
    Writes: total_quote, on_request_services, status
    """
    lead_id: str = state["lead"].lead_id
    tenant_id: str = state["lead"].tenant_id
    mapped_services: List[Dict] = state.get("mapped_services", [])

    log.info(
        "calculator.start",
        lead_id=lead_id,
        tenant_id=tenant_id,
        items=len(mapped_services),
    )

    try:
        total: float = _sum_prices(mapped_services)
    except (KeyError, TypeError, ValueError) as exc:
        log.exception("calculator.error", lead_id=lead_id, error=str(exc))
        return {
            "total_quote": 0.0,
            "status": "error",
            "error_detail": f"Price calculation error: {exc}",
        }

    on_request: List[str] = [
        entry.get("matched_name", entry.get("service", "?"))
        for entry in mapped_services
        if float(entry.get("price", 0.0)) == 0.0
    ]

    log.info(
        "calculator.done",
        lead_id=lead_id,
        tenant_id=tenant_id,
        total_quote=total,
        on_request_count=len(on_request),
    )

    return {
        "total_quote": total,
        "on_request_services": on_request,
        "status": "processing",  # delivery node will set "completed"
        "error_detail": None,
    }
