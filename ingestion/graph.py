"""
ingestion/graph.py
------------------
IngestionGraph — LangGraph subgraph for service catalogue onboarding.

Pipeline topology
-----------------

  ┌─────────────┐
  │  chunker    │  reads file → splits into batches of CHUNK_SIZE rows
  └──────┬──────┘
         │
  ┌──────▼──────┐ ◄──────────────────────────────────────────────────┐
  │  normalizer │  LLM maps raw fields → ServiceItem schema           │
  └──────┬──────┘                                                     │ loop
         │ route_after_normalizer                                      │
         ├── more chunks ─────────────────────────────────────────────┘
         │
         └── all chunks done
                  │
           ┌──────▼──────┐
           │  validator  │  Pydantic checks + business rules
           └──────┬──────┘
                  │ route_after_validator
                  ├── clean (confidence ≥ threshold, no flags) ──────────────┐
                  │                                                            │
                  └── needs review                                             │
                           │                                              ┌────▼──────┐
                    ┌──────▼──────┐                                       │ finalizer │
                    │  approval   │  interrupt() — waits for human        └─────┬─────┘
                    └──────┬──────┘                                             │
                           │ route_after_approval                              END
                           ├── approved ────────────────────────────────────────┘
                           │
                           └── rejected ──► END  (caller re-invokes with feedback)

Key design decisions
--------------------
1. Local LLM only
   The NormalizerNode calls ``_call_openai_compatible`` (Ollama / local server).
   No external API keys required.

2. Multi-tenancy
   Every ChromaDB write uses a collection named ``catalogue_{tenant_id}``.
   Every log line is prefixed with the tenant_id.

3. Chunking loop
   ``current_chunk_index`` advances by 1 per NormalizerNode pass.
   The router decides whether to loop or proceed based on whether all chunks
   have been consumed.  ``normalized_items`` uses ``operator.add`` so it
   accumulates across iterations without replacement.

4. HITL via interrupt()
   ``approval_node`` calls ``interrupt(value=review_payload)`` which suspends
   the graph at that checkpoint.  The human resumes it by calling:

       from langgraph.types import Command
       await graph.ainvoke(
           Command(resume={"approved": True, "feedback": "…"}),
           config={"configurable": {"thread_id": "…"}},
       )

   The value passed to ``resume=`` becomes the return value of ``interrupt()``.

5. AsyncSqliteSaver persistence
   The graph is compiled with an ``AsyncSqliteSaver`` checkpointer so that
   every node transition is persisted.  If the process crashes between nodes
   (or while waiting for human approval), the run can be resumed exactly where
   it left off.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import ValidationError

from core.config import get_settings
from ingestion.models import IngestionState, ServiceItem

logger: logging.Logger = logging.getLogger(__name__)

# ── Pipeline constants ────────────────────────────────────────────────────────

CHUNK_SIZE: int = 50
"""Number of raw rows per normalisation batch."""

CONFIDENCE_THRESHOLD: float = 0.75
"""
Average confidence below which the entire batch is routed to human review.
Individual items auto-flag when their confidence < 0.5 (enforced by ServiceItem).
"""


# ─────────────────────────────────────────────────────────────────────────────
# 1. ChunkingNode
# ─────────────────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> List[Dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _read_json(path: Path) -> List[Dict[str, Any]]:
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    # Support {"items": [...]} or {"data": [...]} wrappers
    for key in ("items", "data", "services", "catalogue", "records"):
        if key in data and isinstance(data[key], list):
            return data[key]
    raise ValueError(
        f"JSON must be an array or an object with a list under a known key "
        f"(items/data/services/catalogue/records).  Got keys: {list(data.keys())}"
    )


def _read_xlsx(path: Path) -> List[Dict[str, Any]]:
    try:
        import openpyxl  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "openpyxl is required for Excel ingestion.  "
            "Install it with: pip install openpyxl"
        ) from exc

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers: List[str] = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
    return [
        {headers[i]: cell for i, cell in enumerate(row)}
        for row in rows[1:]
        if any(cell is not None for cell in row)   # skip blank rows
    ]


def _split_chunks(rows: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


async def chunker_node(state: IngestionState) -> Dict:
    """
    ChunkingNode — reads the source file and splits it into batches.

    Supported formats: CSV, JSON, Excel (xlsx).
    The ``file_format`` field in state is authoritative (not inferred).
    """
    source_file: str = state["source_file"]
    file_format: str = state["file_format"]
    tenant_id: str = state["tenant_id"]
    path: Path = Path(source_file)

    logger.info("[chunker] tenant=%s | file=%s | format=%s", tenant_id, source_file, file_format)

    if not path.exists():
        error_msg = f"[chunker] File not found: {source_file}"
        logger.error(error_msg)
        return {"raw_chunks": [], "sse_logs": [f"[ERROR] {error_msg}"], "error": error_msg}

    try:
        readers = {"csv": _read_csv, "json": _read_json, "xlsx": _read_xlsx}
        if file_format not in readers:
            raise ValueError(f"Unsupported format '{file_format}'. Use csv | json | xlsx.")
        rows: List[Dict[str, Any]] = await asyncio.to_thread(readers[file_format], path)
    except Exception as exc:  # noqa: BLE001
        error_msg = f"[chunker] Failed to read {source_file}: {exc}"
        logger.exception(error_msg)
        return {"raw_chunks": [], "sse_logs": [f"[ERROR] {error_msg}"], "error": error_msg}

    chunks = _split_chunks(rows, CHUNK_SIZE)
    log_entry = (
        f"[CHUNKER] tenant={tenant_id} | rows={len(rows)} | "
        f"chunks={len(chunks)} | chunk_size={CHUNK_SIZE}"
    )
    logger.info(log_entry)

    return {
        "raw_chunks": chunks,
        "current_chunk_index": 0,
        "normalized_items": [],
        "validation_errors": [],
        "flagged_items": [],
        "confidence_score": 0.0,
        "approved": None,
        # NB: NON resettare review_feedback qui. Va preservato dal valore iniziale
        # (make_initial_state) così il NormalizerNode può iniettare il feedback
        # umano nel re-processing HITL (flusso "Correggi e riprocessa").
        "error": None,
        "sse_logs": [log_entry],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. NormalizerNode
# ─────────────────────────────────────────────────────────────────────────────

_NORMALIZER_SYSTEM_PROMPT: str = """
You are a B2B data normalisation agent.  Your job is to map raw service catalogue
rows (which may have inconsistent field names and formats) to a canonical JSON schema.

Target schema for each item (return a JSON array of these objects):
{
  "name":        string  — required, human-readable service name
  "description": string  — optional, free-text description
  "category":    string  — optional, service category / product line
  "price":       number  — required, unit price (must be >= 0; use 0.0 if unknown)
  "currency":    string  — ISO 4217 code, default "EUR"
  "unit":        string  — optional billing unit: "hour"|"month"|"project"|"license"|"user"
  "confidence":  number  — your confidence in this mapping, between 0.0 and 1.0
}

Rules:
- Return ONLY a valid JSON array.  No markdown, no commentary.
- If you cannot confidently map a required field, set confidence <= 0.6.
- Negative prices are invalid; use 0.0 and set confidence <= 0.5.
- Preserve the original currency symbol if present (€ → EUR, $ → USD, £ → GBP).
""".strip()


def _build_normalizer_prompt(
    chunk: List[Dict[str, Any]],
    feedback: Optional[str],
) -> str:
    """Compose the user turn for the normalisation LLM call."""
    base = f"Raw rows to normalise:\n\n{json.dumps(chunk, ensure_ascii=False, indent=2)}"
    if feedback:
        base += (
            f"\n\nHuman reviewer feedback from previous attempt:\n{feedback}\n"
            "Please apply this feedback when re-normalising."
        )
    return base


def _parse_normalizer_response(
    raw: str,
    chunk: List[Dict[str, Any]],
    tenant_id: str,
) -> List[ServiceItem]:
    """
    Parse the LLM JSON response into a list of ``ServiceItem`` objects.

    Handles:
    - Clean JSON arrays
    - Markdown-fenced JSON
    - Partial/malformed JSON (falls back to empty list with a warning)
    """
    text: str = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("[normalizer] Could not parse LLM response as JSON.")
                return []
        else:
            return []

    if not isinstance(parsed, list):
        return []

    items: List[ServiceItem] = []
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            continue
        raw_row: Dict = chunk[i] if i < len(chunk) else {}
        # Sanitize: stringify all keys, convert NaN float values to None.
        # CSV readers (pandas/csv.DictReader) can produce None or float('nan')
        # keys/values which Pydantic rejects in Dict[str, Any].
        safe_raw_data: Dict[str, Any] = {
            str(k): (None if (isinstance(v, float) and v != v) else v)
            for k, v in raw_row.items()
        }
        try:
            item = ServiceItem(
                tenant_id=tenant_id,
                raw_data=safe_raw_data,
                **{k: v for k, v in entry.items() if k != "tenant_id"},
            )
            items.append(item)
        except (ValidationError, TypeError) as exc:
            logger.warning("[normalizer] ServiceItem construction failed for row %d: %s", i, exc)
            # Create a minimal flagged placeholder so the row isn't silently dropped
            items.append(
                ServiceItem(
                    tenant_id=tenant_id,
                    name=str(raw_row.get("name", f"UNKNOWN_ROW_{i}")),
                    price=0.0,
                    confidence=0.0,
                    raw_data=safe_raw_data,
                    flagged=True,
                    flag_reason=f"Construction error: {exc}",
                )
            )
    return items


async def normalizer_node(state: IngestionState) -> Dict:
    """
    NormalizerNode — calls the local LLM to map one raw chunk to ServiceItem list.

    Uses ``_call_openai_compatible`` (Ollama endpoint) via ``asyncio.to_thread``
    for the underlying blocking call, keeping the event loop free.

    On a retry after human rejection, ``state["review_feedback"]`` is injected
    into the prompt so the LLM can correct its previous mapping.
    """
    from agents.extractor import _call_openai_compatible  # noqa: PLC0415 (local import to avoid circularity)

    settings = get_settings()
    tenant_id: str = state["tenant_id"]
    chunks: List[List[Dict]] = state["raw_chunks"]
    idx: int = state["current_chunk_index"]
    feedback: Optional[str] = state.get("review_feedback")

    if idx >= len(chunks):
        # Guard: should not happen due to routing logic, but defensive.
        log_entry = f"[NORMALIZER] tenant={tenant_id} | chunk {idx} out of range — skipping"
        logger.warning(log_entry)
        return {"current_chunk_index": idx + 1, "sse_logs": [log_entry]}

    chunk: List[Dict] = chunks[idx]
    logger.info(
        "[normalizer] tenant=%s | chunk %d/%d | rows=%d | feedback=%s",
        tenant_id, idx + 1, len(chunks), len(chunk), bool(feedback),
    )

    user_prompt: str = _build_normalizer_prompt(chunk, feedback)

    try:
        raw_response: str = await _call_openai_compatible(
            _NORMALIZER_SYSTEM_PROMPT, user_prompt
        )
    except Exception as exc:  # noqa: BLE001
        error_msg = f"[normalizer] LLM call failed (chunk {idx}): {exc}"
        logger.exception(error_msg)
        return {
            "current_chunk_index": idx + 1,
            "normalized_items": [],
            "sse_logs": [f"[ERROR] {error_msg}"],
            "error": error_msg,
        }

    items: List[ServiceItem] = _parse_normalizer_response(raw_response, chunk, tenant_id)
    log_entry = (
        f"[NORMALIZER] tenant={tenant_id} | chunk {idx + 1}/{len(chunks)} | "
        f"items={len(items)} | avg_confidence="
        f"{(sum(i.confidence for i in items) / len(items)):.2f}" if items else "n/a"
    )
    logger.info(log_entry)

    return {
        "current_chunk_index": idx + 1,
        "normalized_items": items,   # operator.add appends to existing list
        "sse_logs": [log_entry],
        "error": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. ValidationNode
# ─────────────────────────────────────────────────────────────────────────────

async def validator_node(state: IngestionState) -> Dict:
    """
    ValidationNode — enforces Pydantic constraints and business rules.

    Checks:
    - Re-validates every ServiceItem (catches any items that slipped through
      the normaliser with invalid data).
    - Flags items with price == 0 and no explanation in the description.
    - Flags items whose name is suspiciously short (< 3 chars).
    - Computes average confidence across all items.
    - Collects all flagged items into ``state["flagged_items"]``.
    """
    tenant_id: str = state["tenant_id"]
    items: List[ServiceItem] = state.get("normalized_items", [])

    logger.info("[validator] tenant=%s | validating %d items", tenant_id, len(items))

    errors: List[str] = []
    flagged: List[ServiceItem] = []
    validated: List[ServiceItem] = []

    for item in items:
        item_errors: List[str] = []

        # Business rule: zero-price items without description are suspicious
        if item.price == 0.0 and not item.description:
            item_errors.append("zero price with no description")

        # Business rule: name too short to be meaningful
        if len(item.name.strip()) < 3:  # noqa: PLR2004
            item_errors.append(f"name too short: '{item.name}'")

        # Business rule: unknown unit values
        known_units = {None, "hour", "month", "project", "license", "user", "day", "year"}
        if item.unit and item.unit not in known_units:
            item_errors.append(f"unrecognised unit: '{item.unit}'")

        if item_errors:
            reason = "; ".join(item_errors)
            # Mutate a copy with updated flag fields
            item = item.model_copy(
                update={"flagged": True, "flag_reason": reason}
            )
            errors.append(f"[{item.id[:8]}] {item.name}: {reason}")

        if item.flagged:
            flagged.append(item)

        validated.append(item)

    confidence: float = (
        sum(i.confidence for i in validated) / len(validated) if validated else 0.0
    )

    log_entry = (
        f"[VALIDATOR] tenant={tenant_id} | total={len(validated)} | "
        f"flagged={len(flagged)} | errors={len(errors)} | avg_confidence={confidence:.2f}"
    )
    logger.info(log_entry)

    # NOTE: Do NOT return "normalized_items" here.
    # That field uses operator.add — returning `validated` would append the
    # entire list a second time, doubling every item.  The flag mutations
    # produced by model_copy are propagated via "flagged_items" instead.
    return {
        "validation_errors": errors,
        "flagged_items": flagged,
        "confidence_score": round(confidence, 4),
        "sse_logs": [log_entry],
        "error": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. ApprovalNode  (HITL — interrupt)
# ─────────────────────────────────────────────────────────────────────────────

async def approval_node(state: IngestionState) -> Dict:
    """
    ApprovalNode — suspends the graph for human review via ``interrupt()``.

    How HITL works in LangGraph
    ---------------------------
    1. This node calls ``interrupt(value=review_payload)``.
    2. LangGraph persists the current checkpoint (via AsyncSqliteSaver) and
       raises an internal ``GraphInterrupt`` exception, which is caught by the
       runtime — the graph is NOT crashed, just suspended.
    3. The caller (API handler) receives the interrupt payload and forwards it
       to the human reviewer (e.g. via SSE or a notification).
    4. When the human has decided, they call:

           from langgraph.types import Command
           result = await graph.ainvoke(
               Command(resume={"approved": True, "feedback": "Looks good."}),
               config={"configurable": {"thread_id": "<thread_id>"}},
           )

       The value of ``resume=`` is what ``interrupt()`` returns inside this node.
    5. The node's return dict updates the state and execution continues from
       the conditional edge after this node.

    The ``review_payload`` surfaced to the human contains only what they need:
    the list of flagged items (with their raw_data for traceability), the
    overall confidence score, and the counts.
    """
    from langgraph.types import interrupt  # noqa: PLC0415

    tenant_id: str = state["tenant_id"]
    flagged: List[ServiceItem] = state.get("flagged_items", [])
    confidence: float = state.get("confidence_score", 0.0)
    total: int = len(state.get("normalized_items", []))

    review_payload: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "source_file": state["source_file"],
        "confidence_score": confidence,
        "total_items": total,
        "flagged_count": len(flagged),
        "flagged_items": [
            {
                "id": item.id,
                "name": item.name,
                "price": item.price,
                "currency": item.currency,
                "flag_reason": item.flag_reason,
                "raw_data": item.raw_data,
            }
            for item in flagged
        ],
        "validation_errors": state.get("validation_errors", []),
    }

    suspend_log = (
        f"[APPROVAL] tenant={tenant_id} | SUSPENDED for human review | "
        f"flagged={len(flagged)} | confidence={confidence:.2f}"
    )
    logger.warning(suspend_log)

    # ── Graph suspends here ───────────────────────────────────────────────────
    # interrupt() never returns in the first execution pass.
    # It only "returns" (with the human's decision dict) after resume.
    decision: Dict[str, Any] = interrupt(value=review_payload)
    # ─────────────────────────────────────────────────────────────────────────

    approved: bool = bool(decision.get("approved", False))
    feedback: Optional[str] = decision.get("feedback") or None

    resume_log = (
        f"[APPROVAL] tenant={tenant_id} | RESUMED | "
        f"decision={'APPROVED' if approved else 'REJECTED'} | "
        f"feedback={feedback!r}"
    )
    logger.info(resume_log)

    return {
        "approved": approved,
        "review_feedback": feedback,
        "sse_logs": [suspend_log, resume_log],
        "error": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. FinalizeNode
# ─────────────────────────────────────────────────────────────────────────────

def _write_to_chroma_sync(
    host: str,
    port: int,
    tenant_id: str,
    items: List[ServiceItem],
) -> int:
    """
    Upsert normalised items into the tenant-scoped ChromaDB collection.

    Collection naming: ``catalogue_{tenant_id}``
    — each tenant's data is fully isolated.

    Documents: concatenation of name + description (for embedding).
    Metadata: all structured fields (price, currency, unit, category, id).
    IDs: the ServiceItem UUID (stable across upserts).

    Returns the number of items written.
    """
    import chromadb  # noqa: PLC0415

    client = chromadb.HttpClient(host=host, port=port)
    collection_name: str = f"catalogue_{tenant_id}"

    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )

    documents: List[str] = []
    metadatas: List[Dict[str, Any]] = []
    ids: List[str] = []

    for item in items:
        doc_text = f"{item.name}"
        if item.description:
            doc_text += f" — {item.description}"
        if item.category:
            doc_text += f" [{item.category}]"

        documents.append(doc_text)
        metadatas.append(
            {
                "service_name": item.name,
                "category": item.category or "",
                "price": item.price,
                "currency": item.currency,
                "unit": item.unit or "",
                "tenant_id": tenant_id,
                "ingested_at": item.ingested_at.isoformat(),
            }
        )
        ids.append(item.id)

    if documents:
        collection.upsert(documents=documents, metadatas=metadatas, ids=ids)

    return len(documents)


async def finalizer_node(state: IngestionState) -> Dict:
    """
    FinalizeNode — persists approved items to ChromaDB.

    Only items that are NOT flagged are written (flagged items were either
    approved as-is by the human or are being dropped pending correction).
    If the human approved, ALL items (including flagged ones) are written,
    since the human confirmed acceptability.
    """
    settings = get_settings()
    tenant_id: str = state["tenant_id"]
    items: List[ServiceItem] = state.get("normalized_items", [])
    approved: Optional[bool] = state.get("approved")

    # Deduplicate by item ID before writing.  The operator.add reducer on
    # normalized_items is safe against node re-runs (chunker → normalizer loops)
    # but a defensive dedup here guards against any future reducer misuse and
    # prevents ChromaDB from raising DuplicateIDError on the same upsert batch.
    seen: Dict[str, ServiceItem] = {}
    for item in items:
        seen.setdefault(item.id, item)  # first occurrence wins
    unique_items: List[ServiceItem] = list(seen.values())

    if len(unique_items) < len(items):
        logger.warning(
            "[finalizer] tenant=%s | deduplicated %d → %d items (dropped %d duplicates)",
            tenant_id, len(items), len(unique_items), len(items) - len(unique_items),
        )

    # If we reached here without going through approval, all items were clean.
    # If we went through approval and got approved=True, write everything.
    items_to_write = unique_items  # human confirmed, write all (deduplicated)

    logger.info(
        "[finalizer] tenant=%s | writing %d items to ChromaDB | approved=%s",
        tenant_id, len(items_to_write), approved,
    )

    try:
        written: int = await asyncio.to_thread(
            _write_to_chroma_sync,
            settings.chroma_host,
            settings.chroma_port,
            tenant_id,
            items_to_write,
        )
    except Exception as exc:  # noqa: BLE001
        error_msg = f"[finalizer] ChromaDB write failed for tenant={tenant_id}: {exc}"
        logger.exception(error_msg)
        return {"sse_logs": [f"[ERROR] {error_msg}"], "error": error_msg}

    log_entry = (
        f"[FINALIZER] tenant={tenant_id} | written={written} | "
        f"collection=catalogue_{tenant_id} | status=OK"
    )
    logger.info(log_entry)

    return {"sse_logs": [log_entry], "error": None}


# ─────────────────────────────────────────────────────────────────────────────
# Conditional routers
# ─────────────────────────────────────────────────────────────────────────────

def route_after_normalizer(
    state: IngestionState,
) -> Literal["normalizer", "validator"]:
    """Loop back to NormalizerNode until all chunks have been processed."""
    if state["current_chunk_index"] < len(state["raw_chunks"]):
        return "normalizer"
    return "validator"


def route_after_validator(
    state: IngestionState,
) -> Literal["approval", "finalizer"]:
    """
    Route to ApprovalNode if:
    - Average confidence is below ``CONFIDENCE_THRESHOLD``, OR
    - At least one item was flagged for review.

    Otherwise skip straight to FinalizeNode.
    """
    needs_review: bool = (
        state.get("confidence_score", 0.0) < CONFIDENCE_THRESHOLD
        or len(state.get("flagged_items", [])) > 0
    )
    return "approval" if needs_review else "finalizer"


def route_after_approval(
    state: IngestionState,
) -> Literal["finalizer", "__end__"]:
    """
    Route to FinalizeNode if the human approved.
    Route to END if rejected — the caller should re-invoke the graph
    with corrected parameters or updated ``review_feedback``.
    """
    if state.get("approved") is True:
        return "finalizer"
    return "__end__"


# ─────────────────────────────────────────────────────────────────────────────
# Graph factory
# ─────────────────────────────────────────────────────────────────────────────

def build_ingestion_graph(checkpointer=None):
    """
    Compile and return the IngestionGraph.

    Parameters
    ----------
    checkpointer : AsyncSqliteSaver | None
        Persistence backend.  Pass ``None`` only in unit tests.
        In production, always provide an ``AsyncSqliteSaver`` so that
        interrupt/resume and crash recovery work correctly.

    Returns
    -------
    CompiledGraph
        Ready-to-invoke compiled LangGraph graph.

    Usage (in an async context)
    ---------------------------
    .. code-block:: python

        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from ingestion.graph import build_ingestion_graph, make_initial_state

        async def run():
            conn = await aiosqlite.connect("data/ingestion.db")
            checkpointer = AsyncSqliteSaver(conn)
            graph = build_ingestion_graph(checkpointer)

            state = make_initial_state(
                tenant_id="acme",
                source_file="/uploads/acme/catalogue.csv",
                file_format="csv",
            )
            config = {"configurable": {"thread_id": "acme-run-001"}}

            # First invocation — runs until interrupt (or completion)
            result = await graph.ainvoke(state, config=config)
    """
    from langgraph.graph import END, StateGraph  # noqa: PLC0415

    builder = StateGraph(IngestionState)

    # ── Register nodes ────────────────────────────────────────────────────────
    builder.add_node("chunker", chunker_node)
    builder.add_node("normalizer", normalizer_node)
    builder.add_node("validator", validator_node)
    builder.add_node("approval", approval_node)
    builder.add_node("finalizer", finalizer_node)

    # ── Static edges ──────────────────────────────────────────────────────────
    builder.set_entry_point("chunker")
    builder.add_edge("chunker", "normalizer")
    builder.add_edge("finalizer", END)

    # ── Conditional edges ─────────────────────────────────────────────────────
    builder.add_conditional_edges(
        source="normalizer",
        path=route_after_normalizer,
        path_map={"normalizer": "normalizer", "validator": "validator"},
    )
    builder.add_conditional_edges(
        source="validator",
        path=route_after_validator,
        path_map={"approval": "approval", "finalizer": "finalizer"},
    )
    builder.add_conditional_edges(
        source="approval",
        path=route_after_approval,
        path_map={"finalizer": "finalizer", "__end__": END},
    )

    compiled = builder.compile(checkpointer=checkpointer)
    logger.info(
        "IngestionGraph compiled (checkpointer=%s)", type(checkpointer).__name__
    )
    return compiled


# ─────────────────────────────────────────────────────────────────────────────
# State initialiser helper
# ─────────────────────────────────────────────────────────────────────────────

def make_initial_state(
    tenant_id: str,
    source_file: str,
    file_format: Literal["csv", "json", "xlsx"],
    review_feedback: Optional[str] = None,
) -> IngestionState:
    """
    Return a clean initial ``IngestionState`` for a new (or re-triggered) run.

    Parameters
    ----------
    tenant_id : str
        Tenant identifier — scopes all DB writes.
    source_file : str
        Absolute path to the file to ingest.
    file_format : "csv" | "json" | "xlsx"
        Explicit format declaration.
    review_feedback : str | None
        Optional feedback from a previous rejected run.
        When provided, NormalizerNode injects it into the LLM prompt.
    """
    return IngestionState(
        tenant_id=tenant_id,
        source_file=source_file,
        file_format=file_format,
        raw_chunks=[],
        current_chunk_index=0,
        normalized_items=[],
        validation_errors=[],
        flagged_items=[],
        confidence_score=0.0,
        approved=None,
        review_feedback=review_feedback,
        sse_logs=[],
        error=None,
    )
