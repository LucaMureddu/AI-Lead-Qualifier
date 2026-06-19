"""
agents/sanitizer.py
-------------------
SanitizerNode — MUST be the first node in the graph.

V2 changes vs V1
----------------
- State type: LeadState → AgentState
- Reads lead text from state["lead"].raw_payload["text"] instead of state["lead_info"].raw_text
- Removed: sse_logs (replaced by structlog + Postgres polling)
- Uses structlog for audit-safe logging (no PII — only lead_id, tenant_id, counts)
"""

from __future__ import annotations

import re
from typing import Dict, List, Pattern, Tuple

import structlog

from core.config import get_settings
from core.state import AgentState

log = structlog.get_logger()

# ── PII regex patterns ────────────────────────────────────────────────────────
_PII_PATTERNS: List[Tuple[str, Pattern[str]]] = [
    ("CARD", re.compile(r"\b(?:\d[ \-]?){13,19}\b")),
    (
        "FISCAL_CODE",
        re.compile(
            r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b",
            re.IGNORECASE,
        ),
    ),
    ("SSN", re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    (
        "PHONE",
        re.compile(
            r"(?:\+\d{1,3}[\s\-]?)?(?:\(?\d{2,4}\)?[\s\-]?)?\d{3,4}[\s\-]?\d{3,6}"
        ),
    ),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}[A-Z0-9]{0,16}\b", re.IGNORECASE)),
]


def _mask_pii(text: str, mask_token: str) -> Tuple[str, int]:
    """Apply all PII patterns and replace matches with mask_token."""
    count: int = 0
    for _label, pattern in _PII_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            text = pattern.sub(mask_token, text)
            count += len(matches)
    return text, count


def sanitizer_node(state: AgentState) -> Dict:
    """
    LangGraph node: mask PII in the lead text, populate sanitized_text.

    Reads: state["lead"].raw_payload["text"]
    Writes: sanitized_text, status (on error: error_detail)
    """
    settings = get_settings()
    lead_id: str = state["lead"].lead_id
    tenant_id: str = state["lead"].tenant_id
    raw_text: str = state["lead"].raw_payload.get("text", "")

    log.info("sanitizer.start", lead_id=lead_id, tenant_id=tenant_id, text_len=len(raw_text))

    try:
        sanitized_text, redaction_count = _mask_pii(raw_text, settings.pii_mask_token)
    except Exception as exc:
        log.exception("sanitizer.error", lead_id=lead_id, error=str(exc))
        return {
            "sanitized_text": "",
            "status": "error",
            "error_detail": f"PII masking failed: {exc}",
        }

    log.info(
        "sanitizer.done",
        lead_id=lead_id,
        tenant_id=tenant_id,
        redactions=redaction_count,
        sanitized_len=len(sanitized_text),
    )

    return {
        "sanitized_text": sanitized_text,
        "status": "processing",
        "error_detail": None,
        # Initialise downstream fields if not already set
        "extracted_services": state.get("extracted_services", []),
        "mapped_services": state.get("mapped_services", []),
        "total_quote": state.get("total_quote", 0.0),
        "retry_count": state.get("retry_count", 0),
    }
