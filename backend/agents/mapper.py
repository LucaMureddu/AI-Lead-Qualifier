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

# Cosine distance threshold for relevance (range: 0.0 = identical, 2.0 = opposite).
# Applied even when settings.mapper_max_distance == 0.0 (disabled) so that
# irrelevant nearest-neighbour results (e.g. "Panino" vs a web-dev catalogue)
# are discarded before reaching the EvaluatorNode.
# Raised to 0.80 for Italian Nomic-Embed-Text: correct B2B matches (e.g.
# "Sito Web Vetrina") typically land around cosine distance 0.60–0.70,
# while true hallucinations ("Panino" vs a web-dev catalogue) sit above 0.85.
# 0.80 keeps valid matches in and discards semantic nonsense.
_DISTANCE_FALLBACK: float = 0.80


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
        except ValueError as exc:
            log.warning(
                "mapper.collection_missing",
                lead_id=lead_id,
                tenant_id=tenant_id,
                error=str(exc),
            )
            return {
                "mapped_services": [],
                "retrieved_docs": [],
                "error_detail": (
                    f"Nessun catalogo trovato per tenant='{tenant_id}'. "
                    f"Run /ingest/stream first. [{exc}]"
                ),
            }
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

        # ── 3. Filtra per distanza e seleziona il MIGLIOR match per query ───────
        # Design: each service_query maps to AT MOST ONE catalogue service.
        #
        # Why best-match-only (not all k results):
        #   • Returning all k=3 nearest-neighbours for 1 extracted service pads
        #     mapped_services with mediocre matches, diluting avg_distance in the
        #     evaluator and pushing the score below the HITL threshold even when
        #     a strong match exists.
        #   • The CalculatorNode would receive 3 prices for 1 requested service,
        #     creating ambiguity in the quote.
        #   • mapped_ratio = min(mapped / extracted, 1.0) is a coverage metric
        #     ("did we find a match for every requested service?"), not a count.
        #     It is only meaningful when mapped ≤ extracted.
        #
        # Log example (pre-fix): extracted=1, mapped=3, avg_dist=0.464 → score=0.54
        # Log example (post-fix): extracted=1, mapped=1, avg_dist=0.25  → score=0.75
        _threshold: float = (
            settings.mapper_max_distance
            if settings.mapper_max_distance > 0.0
            else _DISTANCE_FALLBACK
        )

        # Partition docs into relevant / discarded.
        relevant: List[Document] = []
        for doc in docs:
            dist: float = doc.metadata.get("distance", 1.0)
            if dist > _threshold:
                log.debug(
                    "mapper.doc_discarded",
                    lead_id=lead_id,
                    tenant_id=tenant_id,
                    service=doc.metadata.get("service", "?"),
                    distance=round(dist, 4),
                    threshold=_threshold,
                    query=service_query,
                )
            else:
                relevant.append(doc)

        if not relevant:
            log.debug(
                "mapper.no_relevant_match",
                lead_id=lead_id,
                tenant_id=tenant_id,
                query=service_query,
                threshold=_threshold,
            )
            continue  # No catalogue entry close enough — evaluator will score 0

        # Keep only the best match (lowest cosine distance).
        best: Document = min(relevant, key=lambda d: d.metadata.get("distance", 1.0))
        best_dist: float = best.metadata.get("distance", 1.0)

        retrieved_docs.append(best)
        mapped_services.append(
            {
                "service": best.metadata["service"],
                "matched_name": best.metadata["service"],
                "price": best.metadata["price"],
                "unit": best.metadata.get("unit", "€"),
                # price_type è la colonna tipizzata V3 che sostituisce is_on_request.
                # VARIABLE ⟺ price IS NULL ⟺ "da preventivare".
                # I nodi downstream controllano: entry["price_type"] == "VARIABLE".
                "price_type": best.metadata.get("price_type", "FIXED"),
                "distance": best_dist,
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
