"""
tests/evals/_mapper.py
----------------------
Helper per gli eval del Mapper (RAG su ChromaDB), §4.1.2.

Semina un mini-catalogo deterministico e distintivo in una collezione Chroma
dedicata, poi esegue ``mapper_node`` su query parafrasate. Usato dal Binario B
(live) e da capture_snapshots. Richiede un server ChromaDB attivo.

L'embedder è quello di ChromaDB (all-MiniLM-L6-v2): piccolo, CPU-only,
deterministico — coerente con §4.3.2.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agents.mapper import mapper_node
from core.state import LeadInfo

# Tenant/collezione dedicati agli eval (catalogue_eval_mapper).
SEED_TENANT = "eval_mapper"

# Catalogo minimo e ben separato semanticamente.
SEED_ITEMS: List[Dict[str, Any]] = [
    {
        "id": "svc-web",
        "name": "Sviluppo Sito Web Aziendale",
        "doc": "Sviluppo Sito Web Aziendale — creazione di siti web e landing page",
        "price": 2000.0,
        "unit": "€",
    },
    {
        "id": "svc-seo",
        "name": "Consulenza SEO e Posizionamento",
        "doc": "Consulenza SEO e Posizionamento — ottimizzazione per i motori di ricerca Google",
        "price": 800.0,
        "unit": "€",
    },
    {
        "id": "svc-cloud",
        "name": "Migrazione Cloud",
        "doc": "Migrazione Cloud — spostamento di server e infrastruttura sul cloud",
        "price": 3000.0,
        "unit": "€",
    },
]


def seed_catalog(host: str, port: int, tenant: str = SEED_TENANT, items: List[Dict[str, Any]] = SEED_ITEMS) -> None:
    """(Ri)semina la collezione ``catalogue_{tenant}`` in Chroma. Richiede Chroma attivo."""
    import chromadb  # noqa: PLC0415

    client = chromadb.HttpClient(host=host, port=port)
    name = f"catalogue_{tenant}"
    try:
        client.delete_collection(name)  # slate pulito a ogni run
    except Exception:  # noqa: BLE001  (collezione assente: ok)
        pass
    collection = client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})
    collection.upsert(
        documents=[it["doc"] for it in items],
        metadatas=[{"service_name": it["name"], "price": it["price"], "unit": it["unit"]} for it in items],
        ids=[it["id"] for it in items],
    )


async def run_mapper(services: List[str], tenant: str = SEED_TENANT) -> List[Dict[str, Any]]:
    """Esegue mapper_node sul tenant seminato e ritorna ``mapped_services``."""
    state: Dict[str, Any] = {
        "lead_info": LeadInfo(id="eval-map", raw_text="(eval)", tenant_id=tenant),
        "sanitized_text": "",
        "extracted_services": services,
        "mapped_services": [],
        "total_quote": 0.0,
        "retry_count": 0,
        "sse_logs": [],
        "error": None,
    }
    out = await mapper_node(state)
    return out.get("mapped_services", [])
