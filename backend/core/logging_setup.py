"""
core/logging_setup.py
---------------------
Structlog configuration for B2B AI Lead Qualifier — V2 (Day-2 Observability).

Output: one JSON object per line on stdout.
Docker stdout is captured by Promtail → Loki → Grafana (PLG stack).

PII Policy (ENFORCED via _drop_pii_processor):
  - NEVER log: raw_text, raw_payload contents, messages (full), retrieved_docs (full)
  - ALWAYS safe: tenant_id, lead_id, thread_id, confidence_score, status,
    retry_count, node name, delivery_status, error codes.
  - Lengths/counts are safe: len(messages), len(retrieved_docs).

Grafana alert surface:
  - confidence_score is always emitted by evaluator_node calls so Loki can
    filter/alert on low-confidence thresholds.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

# ── PII keys that must NEVER appear in log output ────────────────────────────

_PII_KEYS: frozenset[str] = frozenset(
    {
        "raw_text",
        "raw_payload",
        "messages",        # full LangChain message objects contain lead text
        "retrieved_docs",  # full Document objects contain tenant corpus text
        "text",            # often a sub-key of raw_payload
        "email",
        "phone",
        "name",
        "address",
    }
)


def _drop_pii_processor(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """
    Structlog processor that strips PII keys before serialisation.

    Drops any top-level key in _PII_KEYS and replaces it with a
    '<key>_REDACTED' sentinel so audits can detect accidental leakage.
    """
    for key in list(event_dict.keys()):
        if key in _PII_KEYS:
            event_dict[f"{key}_REDACTED"] = True
            del event_dict[key]
    return event_dict


def _add_service_context(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Inject static service metadata into every log record."""
    event_dict.setdefault("service", "ai_lead_qualifier")
    event_dict.setdefault("version", "2.0.0")
    return event_dict


# ── Safe AgentState keys for node logging ────────────────────────────────────

#: Keys from AgentState that are safe to include in structured log calls.
#: Nodes should extract only these before logging — never pass state directly.
SAFE_STATE_LOG_KEYS: tuple[str, ...] = (
    "tenant_id",
    "lead_id",
    "thread_id",
    "confidence_score",
    "status",
    "retry_count",
    "delivery_status",
    "delivery_attempts",
    "error_detail",
    # safe count summaries (computed by the caller)
    "messages_count",
    "retrieved_docs_count",
    "extracted_services_count",
    "mapped_services_count",
)


def safe_state_log_context(state: dict[str, Any], thread_id: str = "") -> dict[str, Any]:
    """
    Extract a PII-safe subset of AgentState for structured logging.

    Usage in a LangGraph node::

        from core.logging_setup import safe_state_log_context
        log = structlog.get_logger()

        def my_node(state: AgentState, config: dict) -> dict:
            ctx = safe_state_log_context(state, config["configurable"]["thread_id"])
            log.info("node.start", **ctx)

    Parameters
    ----------
    state:
        The AgentState dict (or any dict that partially mirrors it).
    thread_id:
        The LangGraph thread identifier from config["configurable"]["thread_id"].

    Returns
    -------
    dict with only safe scalar keys populated (missing keys are omitted).
    """
    ctx: dict[str, Any] = {}

    if thread_id:
        ctx["thread_id"] = thread_id

    lead = state.get("lead")
    if lead is not None:
        if hasattr(lead, "tenant_id"):
            ctx["tenant_id"] = lead.tenant_id
            ctx["lead_id"] = lead.lead_id
        elif isinstance(lead, dict):
            ctx["tenant_id"] = lead.get("tenant_id", "")
            ctx["lead_id"] = lead.get("lead_id", "")

    for key in ("confidence_score", "status", "retry_count",
                "delivery_status", "delivery_attempts", "error_detail"):
        val = state.get(key)
        if val is not None:
            ctx[key] = val

    # Replace collections with safe length summaries
    for collection_key in ("messages", "retrieved_docs", "extracted_services", "mapped_services"):
        val = state.get(collection_key)
        if val is not None:
            ctx[f"{collection_key}_count"] = len(val)

    return ctx


# ── Logging configuration ─────────────────────────────────────────────────────

def configure_logging(json_logs: bool = True) -> None:
    """
    Configure structlog + stdlib logging bridge for the application.

    Called once from main.py lifespan on startup.

    Parameters
    ----------
    json_logs:
        True  → JSONRenderer (production / Docker stdout → Promtail).
        False → ConsoleRenderer (local dev pretty-print).
    """
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _drop_pii_processor,
        _add_service_context,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer()
    )

    # Configure structlog itself
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )

    # Wire stdlib root logger so uvicorn / arq / asyncpg emit JSON too
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.INFO)

    # Suppress noisy third-party loggers
    for noisy in ("httpx", "httpcore", "openai", "asyncpg", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
