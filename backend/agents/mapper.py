"""
agents/mapper.py
----------------
MapperNode — ChromaDB lookup via the official Python SDK.

Responsibilities
----------------
1. For each extracted service name, query ChromaDB using ``chromadb.HttpClient``
   (Client/Server mode — never embedded, never in-process).
2. Build ``mapped_services`` as a list of dicts with at least
   ``{"service": str, "price": float, "matched_name": str}``.
3. Append an SSE log entry.

Why the official SDK instead of manual httpx calls?
----------------------------------------------------
Hand-crafted REST calls against ChromaDB's internal HTTP paths (e.g.
``/api/v1/collections/<id>/query``) break when the server's API version
changes — which is exactly the 410 Gone error we hit.  The official
``chromadb.HttpClient`` encapsulates all URL construction and API versioning,
so it stays compatible regardless of the server version.

Why asyncio.to_thread?
----------------------
``chromadb.HttpClient`` is synchronous (blocking I/O).  Calling it directly
inside an ``async def`` node would block the uvicorn event loop.  We offload
it to a thread pool with ``asyncio.to_thread`` — the same pattern used in
``extractor.py`` for the blocking LLM SDKs.

Anti-patterns avoided
---------------------
- chromadb.EphemeralClient / chromadb.Client() (embedded): NEVER used.
- asyncio.run() inside FastAPI: NEVER used.
- Manual REST paths (/api/v1/…): replaced by the versioning-aware SDK.
- PII in queries: only sanitized service names reach ChromaDB.
- Mathematical logic: prices are collected here, summed in CalculatorNode.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import chromadb
from chromadb import Collection, QueryResult

from core.config import get_settings
from core.state import LeadState

logger: logging.Logger = logging.getLogger(__name__)


# ── Sync helper (runs inside asyncio.to_thread) ───────────────────────────────

def _query_chroma_sync(
    host: str,
    port: int,
    collection_name: str,
    query_texts: List[str],
    n_results: int,
) -> QueryResult:
    """
    Open a ``chromadb.HttpClient``, fetch the collection, and run the query.

    This function is **synchronous** and must always be called via
    ``asyncio.to_thread()``.  Instantiating a new client per call is
    intentional: ``HttpClient`` holds a connection pool internally, and
    creating it inside a thread keeps the event loop free.

    Parameters
    ----------
    host, port : str / int
        ChromaDB server coordinates (from settings).
    collection_name : str
        Name of the price-list collection.
    query_texts : list of str
        PII-clean service name strings to embed and search.
    n_results : int
        Maximum nearest neighbours to return per query text.

    Returns
    -------
    QueryResult
        ChromaDB SDK result object (TypedDict) with keys
        ``ids``, ``documents``, ``metadatas``, ``distances``.
    """
    # chromadb.HttpClient è una factory (non una classe): niente annotazione di
    # tipo, lasciamo inferire il ClientAPI ritornato (che ha get_collection).
    client = chromadb.HttpClient(host=host, port=port)
    collection: Collection = client.get_or_create_collection(name=collection_name)
    return collection.query(
        query_texts=query_texts,
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )


# ── Result parser ─────────────────────────────────────────────────────────────

def _extract_best_match(
    result: QueryResult,
    query_index: int,
) -> Optional[Dict[str, Any]]:
    """
    Pick the closest match for a single query from the ``QueryResult``.

    Price-list documents must have metadata of the form:
        {"service_name": "...", "price": 1234.56, "unit": "€"}

    ChromaDB returns results sorted by ascending distance, so index 0 is
    always the best match.

    Returns ``None`` if no results were found for this query.
    """
    try:
        row_meta: List[Dict] = (result["metadatas"] or [])[query_index]  # type: ignore[assignment]
        row_docs: List[str] = (result["documents"] or [])[query_index]
        row_dist: List[float] = (result["distances"] or [])[query_index]

        if not row_meta:
            return None

        best_meta: Dict = row_meta[0]
        best_doc: str = row_docs[0]
        best_dist: float = row_dist[0]

        return {
            "service": best_doc,
            "matched_name": best_meta.get("service_name", best_doc),
            "price": float(best_meta.get("price", 0.0)),
            "unit": best_meta.get("unit", "€"),
            "distance": best_dist,
        }
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("[mapper] Could not extract best match at index %d: %s", query_index, exc)
        return None


# ── Node ─────────────────────────────────────────────────────────────────────

async def mapper_node(state: LeadState) -> Dict:
    """
    LangGraph node: map extracted service names to price-list entries via ChromaDB.

    The blocking ``chromadb.HttpClient`` call is offloaded to a thread with
    ``asyncio.to_thread`` so the uvicorn event loop is never blocked.

    Collection routing
    ------------------
    The collection name is derived from the tenant: ``catalogue_{tenant_id}``.
    This guarantees that each tenant's price list is queried in isolation.
    If the collection does not exist (tenant has no ingested catalogue yet),
    ChromaDB raises ``ValueError``; we surface a clear, actionable error
    message instead of a generic exception.
    """
    settings = get_settings()
    lead_id: str = state["lead_info"].id
    tenant_id: str = state["lead_info"].tenant_id
    collection_name: str = f"catalogue_{tenant_id}"
    extracted: List[str] = state.get("extracted_services", [])

    if not extracted:
        log_entry: str = f"[MAPPER] lead_id={lead_id} | tenant={tenant_id} | no services to map"
        logger.warning(log_entry)
        return {"mapped_services": [], "sse_logs": [log_entry]}

    logger.info(
        "[mapper] Querying collection=%s on %s:%d | lead_id=%s | services=%d",
        collection_name,
        settings.chroma_host,
        settings.chroma_port,
        lead_id,
        len(extracted),
    )

    try:
        result: QueryResult = await asyncio.to_thread(
            _query_chroma_sync,
            settings.chroma_host,
            settings.chroma_port,
            collection_name,
            extracted,
            settings.chroma_n_results,
        )
    except ValueError as exc:
        # ChromaDB raises ValueError when get_collection() is called on a
        # non-existent collection — i.e. the tenant has no ingested catalogue.
        error_msg: str = (
            f"[MAPPER] No catalogue found for tenant='{tenant_id}'. "
            f"Run /ingest/stream first to load a price list. (detail: {exc})"
        )
        logger.error(error_msg)
        return {"mapped_services": [], "sse_logs": [f"[ERROR] {error_msg}"], "error": error_msg}
    except Exception as exc:  # noqa: BLE001
        error_msg = f"[mapper] ChromaDB query failed | collection={collection_name} | lead_id={lead_id}: {exc}"
        logger.exception(error_msg)
        return {"mapped_services": [], "sse_logs": [f"[ERROR] {error_msg}"], "error": error_msg}

    threshold: float = settings.mapper_max_distance
    mapped: List[Dict] = []
    for idx, service_name in enumerate(extracted):
        match = _extract_best_match(result, idx)
        if match and (threshold <= 0.0 or match["distance"] <= threshold):
            mapped.append(match)
            logger.debug(
                "[mapper] '%s' → '%s' @ %.2f%s (dist=%.4f)",
                service_name,
                match["matched_name"],
                match["price"],
                match["unit"],
                match["distance"],
            )
        elif match:
            # Match presente ma OLTRE la soglia di distanza → scartato (off-target).
            # Lasciando mapped_services più povero/vuoto, il router instrada a
            # retry e poi a human_fallback invece di proporre un match irrilevante.
            logger.info(
                "[mapper] '%s' scartato: distanza %.4f > soglia %.2f (off-target)",
                service_name,
                match["distance"],
                threshold,
            )
        else:
            logger.warning("[mapper] No match found for service '%s'", service_name)

    log_entry = (
        f"[MAPPER] tenant={tenant_id} | collection={collection_name} "
        f"| lead_id={lead_id} | queried={len(extracted)} | mapped={len(mapped)}"
    )
    logger.info(log_entry)

    return {
        "mapped_services": mapped,
        "sse_logs": [log_entry],
        "error": None,
    }
