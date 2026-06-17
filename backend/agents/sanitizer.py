"""
agents/sanitizer.py
-------------------
SanitizerNode — MUST be the first node in the graph.

Responsibilities
----------------
1. Mask PII (email, phone, fiscal-code/SSN, credit cards) in ``raw_text``
   before any data reaches the LLM or external services.
2. Append a structured SSE log entry to ``state["sse_logs"]``.
3. Store the cleaned text in ``state["sanitized_text"]``.

Security contract
-----------------
- The original ``raw_text`` is retained in state only for audit purposes;
  all downstream nodes MUST consume ``sanitized_text``.
- No PII ever enters ``sse_logs``, LLM prompts, or ChromaDB queries.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Pattern, Tuple

from core.config import get_settings
from core.state import LeadState

logger: logging.Logger = logging.getLogger(__name__)

# ── PII regex patterns ────────────────────────────────────────────────────────
# Ordered from most specific to least specific to avoid partial matches.
_PII_PATTERNS: List[Tuple[str, Pattern[str]]] = [
    # Credit / debit card numbers (13–19 digits, optional separators)
    ("CARD", re.compile(r"\b(?:\d[ \-]?){13,19}\b")),
    # Italian fiscal code (codice fiscale)
    (
        "FISCAL_CODE",
        re.compile(
            r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b",
            re.IGNORECASE,
        ),
    ),
    # Generic SSN / tax-ID patterns (US: XXX-XX-XXXX)
    ("SSN", re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b")),
    # Email addresses
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # International phone numbers (+39 02 1234567, 06-1234567, etc.)
    (
        "PHONE",
        re.compile(
            r"(?:\+\d{1,3}[\s\-]?)?(?:\(?\d{2,4}\)?[\s\-]?)?\d{3,4}[\s\-]?\d{3,6}"
        ),
    ),
    # IBAN (IT, EU)
    (
        "IBAN",
        re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}[A-Z0-9]{0,16}\b", re.IGNORECASE),
    ),
]


def _mask_pii(text: str, mask_token: str) -> Tuple[str, int]:
    """
    Apply all PII patterns to ``text`` and replace matches with ``mask_token``.

    Returns
    -------
    masked_text : str
    redaction_count : int
        Total number of replacements made (for audit logging).
    """
    count: int = 0

    for _label, pattern in _PII_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            text = pattern.sub(mask_token, text)
            count += len(matches)

    return text, count


# ── Node ──────────────────────────────────────────────────────────────────────

def sanitizer_node(state: LeadState) -> Dict:
    """
    LangGraph node: sanitize ``lead_info.raw_text`` and populate
    ``sanitized_text`` + ``sse_logs``.

    Parameters
    ----------
    state : LeadState
        Current graph state (read-only; return a partial dict to update).

    Returns
    -------
    dict
        Partial state update consumed by LangGraph.
    """
    settings = get_settings()
    lead_id: str = state["lead_info"].id
    raw_text: str = state["lead_info"].raw_text

    logger.info("[sanitizer] Processing lead_id=%s (text_len=%d)", lead_id, len(raw_text))

    try:
        sanitized_text, redaction_count = _mask_pii(raw_text, settings.pii_mask_token)
    except Exception as exc:  # noqa: BLE001
        error_msg: str = f"[sanitizer] PII masking failed for lead_id={lead_id}: {exc}"
        logger.exception(error_msg)
        return {
            "sanitized_text": "",
            "sse_logs": [f"[ERROR] {error_msg}"],
            "error": error_msg,
        }

    log_entry: str = (
        f"[SANITIZER] lead_id={lead_id} | "
        f"redactions={redaction_count} | "
        f"sanitized_len={len(sanitized_text)}"
    )
    logger.info(log_entry)

    return {
        "sanitized_text": sanitized_text,
        "sse_logs": [log_entry],
        # Initialise fields consumed by downstream nodes if not already set.
        "extracted_services": state.get("extracted_services", []),
        "mapped_services": state.get("mapped_services", []),
        "total_quote": state.get("total_quote", 0.0),
        "retry_count": state.get("retry_count", 0),
        "error": None,
    }
