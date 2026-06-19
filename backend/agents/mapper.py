"""
agents/mapper.py
----------------
MapperNode — similarity search su pgvector per la qualificazione lead (V2).

Flusso
------
Per ogni servizio estratto dal lead (``extracted_services``):
1. ``aembed_query()`` genera il vettore di query via Ollama (asincrono).
2. ``similarity_search()`` recupera i servizi più vicini nel catalogo pgvector.
3. I risultati vengono accumulati in ``mapped_services`` e ``retrieved_docs``.

V2 vs V1
--------
- ChromaDB + asyncio.to_thread  →  database.vector_store.similarity_search (asyncpg nativo)
- stub zero-vector _embed()     →  services.embeddings.aembed_query (Ollama reale)
- LeadState / LeadInfo          →  AgentState / LeadContext
- sse_logs                      →  rimossi; usa structlog
"""

from __future__ import annotations

from typing import Dict, List

import structlog
from langchain_core.documents import Document

from core.config import get_settings
from core.state import AgentState
from database.vector_store import similarity_search
from services.embeddings import EmbeddingError, aembed_query

log: structlog.BoundLogger = structlog.get_logger()


async def mapper_node(state: AgentState) -> Dict:
    """
    LangGraph node: mappa i servizi estratti al catalogo pgvector via embedding.

    Legge da:  state["lead"].tenant_id, state["extracted_services"]
    Scrive su: mapped_services, retrieved_docs, (in errore) error_detail

    Sicurezza dei log
    -----------------
    Le chiamate a ``aembed_query`` loggano solo ``text_len`` e ``tenant_id``
    in caso di errore — mai il testo del servizio (potenziale PII).
    """
    settings = get_settings()
    lead_id: str = state["lead"].lead_id
    tenant_id: str = state["lead"].tenant_id
    extracted: List[str] = state.get("extracted_services", [])

    if not extracted:
        log.warning("mapper.no_services", lead_id=lead_id, tenant_id=tenant_id)
        return {"mapped_services": [], "retrieved_docs": []}

    log.info(
        "mapper.start",
        lead_id=lead_id,
        tenant_id=tenant_id,
        services_count=len(extracted),
    )

    mapped_services: List[Dict] = []
    retrieved_docs: List[Document] = []

    for service_query in extracted:
        # ── 1. Embedding asincrono via Ollama ─────────────────────────────────
        try:
            embedding: List[float] = await aembed_query(
                service_query, tenant_id=tenant_id
            )
        except EmbeddingError as exc:
            # Errore di connessione/timeout verso Ollama: non ritentare,
            # segnalare immediatamente l'errore al grafo per routing HITL.
            log.error(
                "mapper.embed_failed",
                lead_id=lead_id,
                tenant_id=tenant_id,
                error=str(exc),
            )
            return {
                "mapped_services": [],
                "retrieved_docs": [],
                "error_detail": (
                    f"Embedding fallito per tenant='{tenant_id}': {exc}"
                ),
            }

        # ── 2. Similarity search su pgvector ──────────────────────────────────
        try:
            docs: List[Document] = await similarity_search(
                query_embedding=embedding,
                tenant_id=tenant_id,
                max_distance=(
                    settings.mapper_max_distance
                    if settings.mapper_max_distance > 0.0
                    else None
                ),
            )
        except Exception as exc:
            log.exception(
                "mapper.query_error",
                lead_id=lead_id,
                tenant_id=tenant_id,
                error=str(exc),
            )
            return {
                "mapped_services": [],
                "retrieved_docs": [],
                "error_detail": (
                    f"pgvector query fallita per tenant='{tenant_id}': {exc}"
                ),
            }

        # ── 3. Accumula risultati ─────────────────────────────────────────────
        retrieved_docs.extend(docs)
        for doc in docs:
            mapped_services.append(
                {
                    "service": doc.metadata["service"],
                    "matched_name": doc.metadata["service"],
                    "price": doc.metadata["price"],
                    "unit": doc.metadata.get("unit", "€"),
                    "distance": doc.metadata["distance"],
                    "query": service_query,
                }
            )

    log.info(
        "mapper.done",
        lead_id=lead_id,
        tenant_id=tenant_id,
        extracted=len(extracted),
        mapped=len(mapped_services),
        retrieved_docs=len(retrieved_docs),
    )

    return {
        "mapped_services": mapped_services,
        "retrieved_docs": retrieved_docs,
        "error_detail": None,
    }
