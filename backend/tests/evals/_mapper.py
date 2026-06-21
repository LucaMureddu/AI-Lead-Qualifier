"""
tests/evals/_mapper.py
----------------------
Helper per gli eval del Mapper (RAG su pgvector), §4.1.2.

Semina un mini-catalogo deterministico e distintivo in pgvector via
``database.vector_store.upsert_items``, poi esegue ``mapper_node`` su query
parafrasate. Usato dal Binario B (live) e da capture_snapshots.
Richiede Postgres+pgvector attivo e Ollama (per gli embedding).

V2: migrato da ChromaDB a pgvector — coerente con la migrazione del mapper.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agents.mapper import mapper_node
from core.state import LeadContext
from database.vector_store import upsert_items, wipe_tenant
from services.embeddings import get_embeddings_service

# Tenant dedicato agli eval del mapper.
SEED_TENANT = "eval_mapper"

# Catalogo minimo e ben separato semanticamente.
SEED_ITEMS: List[Dict[str, Any]] = [
    {
        "name": "Sviluppo Sito Web Aziendale",
        "description": "Sviluppo Sito Web Aziendale — creazione di siti web e landing page",
        "price": 2000.0,
        "price_type": "fixed",
    },
    {
        "name": "Consulenza SEO e Posizionamento",
        "description": "Consulenza SEO e Posizionamento — ottimizzazione per i motori di ricerca Google",
        "price": 800.0,
        "price_type": "fixed",
    },
    {
        "name": "Migrazione Cloud",
        "description": "Migrazione Cloud — spostamento di server e infrastruttura sul cloud",
        "price": 3000.0,
        "price_type": "fixed",
    },
]


async def seed_catalog(tenant: str = SEED_TENANT, items: List[Dict[str, Any]] = SEED_ITEMS) -> None:
    """(Ri)semina il catalogo di eval in pgvector. Richiede Postgres+pgvector e Ollama attivi."""
    embedder = get_embeddings_service()
    await wipe_tenant(tenant)
    docs = [it["description"] for it in items]
    embeddings = await embedder.aembed_documents(docs)
    rows = [
        {
            "service": it["name"],
            "price": it["price"],
            "price_type": it["price_type"],
            "description": it["description"],
            "embedding": emb,
            "metadata": {},
        }
        for it, emb in zip(items, embeddings)
    ]
    await upsert_items(rows, tenant_id=tenant)


async def run_mapper(services: List[str], tenant: str = SEED_TENANT) -> List[Dict[str, Any]]:
    """Esegue mapper_node sul tenant seminato e ritorna ``mapped_services``."""
    state: Dict[str, Any] = {
        "lead": LeadContext(lead_id="eval-map", tenant_id=tenant, raw_payload={"text": "(eval)"}),
        "messages": [],
        "retrieved_docs": [],
        "confidence_score": 0.0,
        "human_approved": None,
        "review_feedback": None,
        "status": "queued",
        "error_detail": None,
        "sanitized_text": "",
        "extracted_services": services,
        "mapped_services": [],
        "total_quote": 0.0,
        "on_request_services": [],
        "retry_count": 0,
        "delivery_status": "PENDING",
        "delivery_attempts": 0,
        "delivery_error": None,
    }
    out = await mapper_node(state)
    return out.get("mapped_services", [])
