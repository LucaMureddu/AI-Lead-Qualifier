"""
agents/calculator.py
--------------------
CalculatorNode — pure Python, zero LLM.

Responsibilities
----------------
1. Sum the ``price`` field of every entry in ``mapped_services``.
2. Write the result to ``total_quote``.
3. Append a structured SSE log entry.

Design contract
---------------
- No external calls (API, DB, filesystem).
- No LLM involvement — deterministic, testable, auditable.
- Raises ValueError on malformed input rather than silently returning 0.
"""

from __future__ import annotations

import logging
from typing import Dict, List

from core.state import LeadState

logger: logging.Logger = logging.getLogger(__name__)


def _sum_prices(mapped_services: List[Dict]) -> float:
    """
    Sum the ``price`` field across all mapped service entries.

    Parameters
    ----------
    mapped_services : list of dicts
        Each dict must contain a numeric ``price`` key.

    Returns
    -------
    float
        Total price (rounded to 2 decimal places).

    Raises
    ------
    KeyError
        If a service dict is missing the ``price`` key.
    TypeError
        If a ``price`` value cannot be converted to float.
    """
    total: float = 0.0
    for entry in mapped_services:
        price: float = float(entry["price"])
        total += price
    return round(total, 2)


def calculator_node(state: LeadState) -> Dict:
    """
    LangGraph node: compute the total quote from ``mapped_services``.

    Parameters
    ----------
    state : LeadState
        Current graph state.

    Returns
    -------
    dict
        Partial state update with ``total_quote`` and appended ``sse_logs``.
    """
    lead_id: str = state["lead_info"].id
    mapped_services: List[Dict] = state.get("mapped_services", [])

    logger.info(
        "[calculator] Computing quote for lead_id=%s | items=%d",
        lead_id,
        len(mapped_services),
    )

    try:
        total: float = _sum_prices(mapped_services)
    except (KeyError, TypeError, ValueError) as exc:
        error_msg: str = (
            f"[calculator] Price calculation error for lead_id={lead_id}: {exc}"
        )
        logger.exception(error_msg)
        return {
            "total_quote": 0.0,
            "sse_logs": [f"[ERROR] {error_msg}"],
            "error": error_msg,
        }

    # Build a readable breakdown for the SSE stream.
    breakdown_lines: List[str] = [
        f"  • {entry.get('matched_name', entry.get('service', '?'))} "
        f"→ {entry.get('price', 0.0):.2f} {entry.get('unit', '€')}"
        for entry in mapped_services
    ]
    breakdown: str = "\n".join(breakdown_lines)

    log_entry: str = (
        f"[CALCULATOR] lead_id={lead_id} | total_quote={total:.2f}€\n{breakdown}"
    )
    logger.info(log_entry)

    return {
        "total_quote": total,
        "sse_logs": [log_entry],
        "error": None,
    }
