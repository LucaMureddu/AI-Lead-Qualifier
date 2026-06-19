"""
agents/extractor.py
-------------------
ExtractorNode — LLM-powered service extraction (async).

V2 changes vs V1
----------------
- State type: LeadState → AgentState
- Reads lead_id from state["lead"].lead_id instead of state["lead_info"].id
- Removed: sse_logs; uses structlog instead
- Uses structlog for audit-safe logging (no PII in logs)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import httpx
import structlog

from core.config import get_settings
from core.state import AgentState

log = structlog.get_logger()

# ── Prompt template ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT: str = (
    "You are a B2B sales assistant. "
    "Extract a JSON array of professional service names explicitly or implicitly requested "
    "in the following text. "
    "CRITICAL LANGUAGE RULE: YOU MUST PRESERVE THE ORIGINAL LANGUAGE OF THE REQUEST. "
    "If the user writes in Italian, extract service names IN ITALIAN. "
    "If the user writes in English, extract in English. NEVER translate. "
    "Return ONLY a valid JSON array of strings, no commentary, no markdown fences. "
    "Example (Italian input): [\"Sviluppo Web\", \"Manutenzione Server\", \"Migrazione Cloud\"] "
    "Example (English input): [\"Web Development\", \"SEO Audit\", \"Cloud Migration\"]"
)


def _build_user_prompt(sanitized_text: str, previous_services: List[str]) -> str:
    base: str = f"Lead request:\n\n{sanitized_text}"
    if previous_services:
        feedback: str = (
            "\n\nPrevious extraction attempt returned the following services that "
            f"could NOT be mapped to the price list: {json.dumps(previous_services)}. "
            "Please refine your extraction — use broader or alternative service names."
        )
        return base + feedback
    return base


# ── LLM adapters ─────────────────────────────────────────────────────────────

async def _call_openai_compatible(prompt_system: str, prompt_user: str) -> str:
    settings = get_settings()
    url: str = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    payload: Dict[str, Any] = {
        "model": settings.llm_model_name,
        "messages": [
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": prompt_user},
        ],
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
    data: Dict[str, Any] = response.json()
    return data["choices"][0]["message"]["content"]


def _call_groq(prompt_system: str, prompt_user: str) -> str:
    from groq import Groq  # type: ignore[import]
    settings = get_settings()
    client = Groq(api_key=settings.groq_api_key)
    response = client.chat.completions.create(
        model=settings.groq_model,
        messages=[
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": prompt_user},
        ],
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
    return response.choices[0].message.content or ""



# ── Llama model singleton ─────────────────────────────────────────────────────
# Loading a GGUF model from disk is expensive: it allocates GPU/CPU memory and
# takes hundreds of milliseconds. Re-loading it on every extractor call (or on
# every ARQ job) would saturate memory and destroy throughput.
# This singleton is initialised once per process (ARQ worker or FastAPI) and
# shared across all subsequent calls. Not thread-safe for parallel invocations,
# but ARQ workers run one job at a time (ARQ_MAX_JOBS=1) so concurrent access
# is not a concern in the default configuration.

_llama_instance: "Any | None" = None


def _get_llama_model() -> "Any":
    """
    Return the process-scoped Llama model, loading it on first call.

    Model path and context window come from Settings — nothing is hardcoded.
    A structured log event is emitted only at load time, not on every call.

    Raises
    ------
    RuntimeError
        If the GGUF file does not exist at the configured path.
    """
    global _llama_instance
    if _llama_instance is None:
        from llama_cpp import Llama  # type: ignore[import]

        settings = get_settings()
        model_path: str = str(settings.llm_model_path)
        if not settings.llm_model_path.exists():
            raise RuntimeError(
                f"GGUF model not found at '{model_path}'. "
                "Set LLM_MODEL_PATH to a valid .gguf file path."
            )
        log.info("llama.model_loading", model_path=model_path)
        _llama_instance = Llama(model_path=model_path, n_ctx=2048, verbose=False)
        log.info("llama.model_ready", model_path=model_path)
    return _llama_instance


def _call_llama(prompt_system: str, prompt_user: str) -> str:
    """Call the local GGUF model via the process-scoped singleton."""
    settings = get_settings()
    llm = _get_llama_model()
    response = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": prompt_user},
        ],
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
    return response["choices"][0]["message"]["content"]  # type: ignore[index, return-value]


def _invoke_llm_blocking(prompt_system: str, prompt_user: str) -> str:
    settings = get_settings()
    if settings.llm_provider == "groq":
        return _call_groq(prompt_system, prompt_user)
    return _call_llama(prompt_system, prompt_user)


# ── Response parser ───────────────────────────────────────────────────────────

def _parse_services(raw_response: str) -> List[str]:
    text: str = raw_response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                log.warning("extractor.parse_failed")
                return []
        else:
            return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


# ── Node ──────────────────────────────────────────────────────────────────────

async def extractor_node(state: AgentState) -> Dict:
    """
    LangGraph node: extract service names from sanitized_text via LLM.

    Reads: state["lead"].lead_id, state["sanitized_text"], state["retry_count"]
    Writes: extracted_services, retry_count, (on parse failure) error_detail

    Error handling
    --------------
    - Network / connection / HTTP errors  → raise RuntimeError (ARQ retries the job,
      LangGraph state is NOT touched, retry_count is NOT consumed).
    - LLM responds but output unparseable → return state with retry_count + 1
      (logical failure, graph retries or escalates to HITL).
    """
    settings = get_settings()
    lead_id: str = state["lead"].lead_id
    tenant_id: str = state["lead"].tenant_id
    retry_count: int = state.get("retry_count", 0)
    previous: List[str] = state.get("extracted_services", []) if retry_count > 0 else []
    sanitized_text: str = state.get("sanitized_text", "")

    if not sanitized_text:
        log.error("extractor.empty_text", lead_id=lead_id)
        return {
            "extracted_services": [],
            "error_detail": "sanitized_text is empty",
        }

    user_prompt: str = _build_user_prompt(sanitized_text, previous)

    log.info(
        "extractor.start",
        lead_id=lead_id,
        tenant_id=tenant_id,
        provider=settings.llm_provider,
        retry=retry_count,
    )

    # ── Call LLM ──────────────────────────────────────────────────────────────
    # Network / connection / HTTP errors are NOT logical failures of the graph:
    # they are infrastructure failures. We raise RuntimeError so ARQ fails the
    # job and retries it natively — without touching LangGraph state and without
    # consuming a retry_count slot.
    #
    # Only a *successful* LLM response that produces unusable output (empty
    # array, malformed JSON) is a logical failure that increments retry_count
    # and may eventually escalate to HITL.
    try:
        if settings.llm_provider == "openai":
            raw_response: str = await _call_openai_compatible(_SYSTEM_PROMPT, user_prompt)
        else:
            raw_response = await asyncio.to_thread(
                _invoke_llm_blocking, _SYSTEM_PROMPT, user_prompt
            )
    except httpx.RequestError as exc:
        # Connection refused, timeout, DNS failure — transient network issue.
        log.error("extractor.network_error", lead_id=lead_id, error=str(exc))
        raise RuntimeError(f"LLM network error: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        # 4xx / 5xx from the LLM endpoint — treat as transient infrastructure failure.
        log.error(
            "extractor.http_error",
            lead_id=lead_id,
            status_code=exc.response.status_code,
            error=str(exc),
        )
        raise RuntimeError(f"LLM HTTP {exc.response.status_code}: {exc}") from exc
    except Exception as exc:
        # Any other provider error (groq, llama_cpp SDK, etc.) — infrastructure,
        # not a graph-level logical failure.
        log.exception("extractor.llm_call_failed", lead_id=lead_id, error=str(exc))
        raise RuntimeError(f"LLM call failed: {exc}") from exc

    # ── Parse LLM output ──────────────────────────────────────────────────────
    # The network call succeeded. If parsing yields nothing, that is a *logical*
    # failure (model returned malformed JSON or an empty array). Increment
    # retry_count so the graph can retry extraction or escalate to HITL.
    services: List[str] = _parse_services(raw_response)

    log.info(
        "extractor.done",
        lead_id=lead_id,
        tenant_id=tenant_id,
        provider=settings.llm_provider,
        retry=retry_count,
        services_count=len(services),
    )

    return {
        "extracted_services": services,
        "retry_count": retry_count + 1,
        "error_detail": None if services else "LLM response parsed but no services extracted",
    }
