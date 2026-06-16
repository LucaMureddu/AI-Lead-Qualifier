"""
agents/extractor.py
-------------------
ExtractorNode — LLM-powered service extraction (async).

Responsibilities
----------------
1. Build a prompt from the PII-clean ``sanitized_text``.
2. Dispatch to the configured LLM backend:
   - "openai"  → any OpenAI-compatible server (Ollama, LM Studio, vLLM, …)
                  via native async httpx.AsyncClient — no thread offload needed.
   - "groq"    → Groq cloud API (blocking SDK, offloaded with asyncio.to_thread).
   - "llama"   → local GGUF via llama-cpp-python (blocking, asyncio.to_thread).
3. Parse the response into a ``List[str]`` of service names.
4. Increment ``retry_count`` when called on a retry loop.
5. Append an SSE log entry.

Anti-patterns avoided
---------------------
- Raw PII never enters the prompt (SanitizerNode runs first).
- No mathematical computation here — that belongs to CalculatorNode.
- asyncio.run() is never used; all async paths stay in the running event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

import httpx

from core.config import get_settings
from core.state import LeadState

logger: logging.Logger = logging.getLogger(__name__)

# ── Prompt template ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT: str = (
    "You are a B2B sales assistant. "
    "Extract a JSON array of professional service names explicitly or implicitly requested "
    "in the following text. "
    "Return ONLY a valid JSON array of strings, no commentary, no markdown fences. "
    "Example output: [\"Web Development\", \"SEO Audit\", \"Cloud Migration\"]"
)


def _build_user_prompt(sanitized_text: str, previous_services: List[str]) -> str:
    """Compose the user turn, optionally including feedback from a previous attempt."""
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

# ── Async: OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, …) ──────────

async def _call_openai_compatible(prompt_system: str, prompt_user: str) -> str:
    """
    POST to any OpenAI-compatible ``/chat/completions`` endpoint using
    ``httpx.AsyncClient``.

    This is the primary, natively async path — no thread offload required.
    Compatible with Ollama (default port 11434), LM Studio (port 1234),
    vLLM, and any other server that implements the OpenAI chat API.

    Parameters
    ----------
    prompt_system : str
        System-role message content.
    prompt_user : str
        User-role message content (PII-clean).

    Returns
    -------
    str
        The model's text reply.

    Raises
    ------
    httpx.HTTPStatusError
        On 4xx / 5xx responses from the inference server.
    httpx.RequestError
        On network-level failures (connection refused, timeout, …).
    """
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

    logger.debug("[extractor/openai] POST %s model=%s", url, settings.llm_model_name)

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()

    data: Dict[str, Any] = response.json()
    content: str = data["choices"][0]["message"]["content"]
    return content


# ── Sync (blocking): Groq cloud ───────────────────────────────────────────────

def _call_groq(prompt_system: str, prompt_user: str) -> str:
    """
    Call the Groq cloud API via the official SDK (blocking I/O).
    Intended to be offloaded with ``asyncio.to_thread`` from the node.
    """
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


# ── Sync (blocking): local GGUF via llama-cpp-python (legacy) ────────────────

def _call_llama(prompt_system: str, prompt_user: str) -> str:
    """
    Load and call a local GGUF model via llama-cpp-python (blocking I/O).
    Intended to be offloaded with ``asyncio.to_thread`` from the node.
    """
    from llama_cpp import Llama  # type: ignore[import]

    settings = get_settings()
    llm = Llama(
        model_path=str(settings.llm_model_path),
        n_ctx=2048,
        verbose=False,
    )
    response = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": prompt_user},
        ],
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
    return response["choices"][0]["message"]["content"]


# ── Sync dispatcher for blocking backends ─────────────────────────────────────

def _invoke_llm_blocking(prompt_system: str, prompt_user: str) -> str:
    """
    Dispatch to a *blocking* backend (groq or llama).
    Called via ``asyncio.to_thread`` from the node so the event loop stays free.
    The "openai" provider is handled separately as a native coroutine.
    """
    settings = get_settings()
    if settings.llm_provider == "groq":
        return _call_groq(prompt_system, prompt_user)
    # "llama" (legacy GGUF)
    return _call_llama(prompt_system, prompt_user)


# ── Response parser ───────────────────────────────────────────────────────────

def _parse_services(raw_response: str) -> List[str]:
    """
    Parse the LLM response into a list of service name strings.

    Handles:
    - Clean JSON arrays
    - Arrays wrapped in markdown code fences
    - Partial / malformed JSON (returns empty list)
    """
    text: str = raw_response.strip()

    # Strip optional markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Attempt to extract a JSON array substring
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("[extractor] Could not parse LLM response as JSON.")
                return []
        else:
            return []

    if not isinstance(parsed, list):
        return []

    return [str(item).strip() for item in parsed if str(item).strip()]


# ── Node ──────────────────────────────────────────────────────────────────────

async def extractor_node(state: LeadState) -> Dict:
    """
    LangGraph node: extract service names from ``sanitized_text`` via LLM.

    Provider routing
    ----------------
    - "openai"  → ``await _call_openai_compatible(...)``
                  Native coroutine; httpx handles the I/O inside the event loop.
                  No thread needed.
    - "groq"    → ``await asyncio.to_thread(_invoke_llm_blocking, ...)``
                  The Groq SDK is synchronous; offloaded to a thread pool.
    - "llama"   → ``await asyncio.to_thread(_invoke_llm_blocking, ...)``
                  llama-cpp-python is synchronous; offloaded to a thread pool.

    On a retry loop (retry_count > 0), the previous ``extracted_services``
    are passed back to the LLM as negative feedback to guide re-extraction.
    """
    settings = get_settings()
    lead_id: str = state["lead_info"].id
    retry_count: int = state.get("retry_count", 0)

    # On retry, pass the previously extracted (unmappable) services as feedback.
    previous: List[str] = state.get("extracted_services", []) if retry_count > 0 else []

    sanitized_text: str = state.get("sanitized_text", "")
    if not sanitized_text:
        error_msg: str = f"[extractor] sanitized_text is empty for lead_id={lead_id}"
        logger.error(error_msg)
        return {"extracted_services": [], "sse_logs": [f"[ERROR] {error_msg}"], "error": error_msg}

    user_prompt: str = _build_user_prompt(sanitized_text, previous)

    logger.info(
        "[extractor] Calling LLM (provider=%s) for lead_id=%s (retry=%d)",
        settings.llm_provider,
        lead_id,
        retry_count,
    )

    try:
        if settings.llm_provider == "openai":
            # Native async — no thread offload required.
            raw_response: str = await _call_openai_compatible(_SYSTEM_PROMPT, user_prompt)
        else:
            # Blocking SDK (groq / llama) — offload to thread pool.
            raw_response = await asyncio.to_thread(
                _invoke_llm_blocking, _SYSTEM_PROMPT, user_prompt
            )

        services: List[str] = _parse_services(raw_response)

    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        error_msg = (
            f"[extractor] OpenAI-compatible endpoint error for lead_id={lead_id}: {exc}"
        )
        logger.exception(error_msg)
        return {
            "extracted_services": [],
            "retry_count": retry_count + 1,
            "sse_logs": [f"[ERROR] {error_msg}"],
            "error": error_msg,
        }
    except Exception as exc:  # noqa: BLE001
        error_msg = f"[extractor] LLM call failed for lead_id={lead_id}: {exc}"
        logger.exception(error_msg)
        return {
            "extracted_services": [],
            "retry_count": retry_count + 1,
            "sse_logs": [f"[ERROR] {error_msg}"],
            "error": error_msg,
        }

    log_entry: str = (
        f"[EXTRACTOR] lead_id={lead_id} | provider={settings.llm_provider} | "
        f"retry={retry_count} | services_found={len(services)} | services={services}"
    )
    logger.info(log_entry)

    return {
        "extracted_services": services,
        "retry_count": retry_count + 1,
        "sse_logs": [log_entry],
        "error": None,
    }
