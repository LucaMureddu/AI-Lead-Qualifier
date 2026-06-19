"""
scripts/test_embeddings.py
---------------------------
Smoke test asincrono: verifica il round-trip completo
embedding Ollama → pgvector insert → similarity_search.

Prerequisiti
------------
1. Ollama in esecuzione con il modello scaricato::

       ollama pull nomic-embed-text

2. Postgres+pgvector avviato con schema applicato::

       docker compose up -d postgres
       cd backend/ && alembic upgrade head

3. Variabile d'ambiente (opzionale — usa i default se non impostata)::

       export DATABASE_DSN=postgresql://app:password@localhost/ai_lead_qualifier
       export EMBEDDING_BASE_URL=http://localhost:11434
       export EMBEDDING_MODEL=nomic-embed-text

Esecuzione
----------
::

    cd backend/
    python scripts/test_embeddings.py

Output atteso (successo)
------------------------
::

    [1/3] Test generazione embedding...
          ✓ vettore dim=768, campione=[-0.0123, 0.0456, ...]
    [2/3] Inserimento in pgvector (tenant=smoke_test)...
          ✓ 3 servizi scritti
    [3/3] Similarity search...
          ✓ Risultato 1: "Web Development"  distance=0.0231
          ✓ Risultato 2: "Email Setup"      distance=0.6789
    Cleanup: 3 righe eliminate.
    ✓ Smoke test completato.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Aggiunge backend/ al path per poter importare i moduli del progetto
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg

from core.config import get_settings
from database.db_core import close_pool, get_pool
from database.vector_store import similarity_search, upsert_items, wipe_tenant
from services.embeddings import (
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    aembed_documents,
    aembed_query,
    get_embeddings_service,
)

_TENANT: str = "smoke_test"

_TEST_SERVICES: list[dict] = [
    {"name": "Web Development",  "description": "Sviluppo siti web e landing page."},
    {"name": "Email Setup",      "description": "Configurazione server di posta."},
    {"name": "Cloud Hosting",    "description": "Hosting su infrastruttura cloud."},
]

_QUERY_TEXT: str = "sito web aziendale per startup"


async def main() -> None:
    settings = get_settings()
    print(
        f"\nConfig:\n"
        f"  model            = {settings.embedding_model}\n"
        f"  embedding_base_url = {settings.embedding_base_url}\n"
        f"  pgvector_dim     = {settings.pgvector_embedding_dim}\n"
        f"  database_dsn     = {settings.database_dsn!r}\n"
    )

    # ── 1. Generazione embedding singolo ──────────────────────────────────────
    print("[1/3] Test generazione embedding...")
    try:
        vec: list[float] = await aembed_query(_QUERY_TEXT, tenant_id=_TENANT)
    except EmbeddingError as exc:
        print(f"  ✗ ERRORE: {exc}")
        print(
            "  → Verificare che Ollama sia avviato e il modello sia disponibile:\n"
            f"    ollama pull {settings.embedding_model}"
        )
        sys.exit(1)
    except EmbeddingDimensionMismatchError as exc:
        print(f"  ✗ MISMATCH DIM: {exc}")
        sys.exit(1)

    sample: str = str([round(v, 4) for v in vec[:3]])
    print(f"  ✓ vettore dim={len(vec)}, campione={sample}")

    # ── 2. Batch embedding + upsert in pgvector ───────────────────────────────
    print(f"\n[2/3] Inserimento in pgvector (tenant={_TENANT!r})...")
    texts: list[str] = [
        f"{s['name']} — {s['description']}" for s in _TEST_SERVICES
    ]
    try:
        embeddings: list[list[float]] = await aembed_documents(texts, tenant_id=_TENANT)
    except (EmbeddingError, EmbeddingDimensionMismatchError) as exc:
        print(f"  ✗ ERRORE batch embedding: {exc}")
        sys.exit(1)

    items_to_upsert: list[dict] = [
        {
            "service": svc["name"],
            "price": float((i + 1) * 100),
            "description": texts[i],
            "embedding": embeddings[i],
        }
        for i, svc in enumerate(_TEST_SERVICES)
    ]

    try:
        written: int = await upsert_items(items_to_upsert, tenant_id=_TENANT)
    except Exception as exc:
        print(f"  ✗ ERRORE upsert pgvector: {exc}")
        print("  → Verificare che Postgres sia avviato e alembic upgrade head sia stato eseguito.")
        sys.exit(1)

    print(f"  ✓ {written} servizi scritti")

    # ── 3. Similarity search ──────────────────────────────────────────────────
    print(f"\n[3/3] Similarity search (query: {_QUERY_TEXT!r})...")
    from langchain_core.documents import Document

    try:
        results: list[Document] = await similarity_search(
            query_embedding=vec,
            tenant_id=_TENANT,
            n_results=3,
        )
    except Exception as exc:
        print(f"  ✗ ERRORE similarity_search: {exc}")
        sys.exit(1)

    if not results:
        print("  ✗ Nessun risultato ritornato — pgvector potrebbe non avere dati.")
        sys.exit(1)

    for i, doc in enumerate(results, 1):
        service: str = doc.metadata["service"]
        distance: float = doc.metadata["distance"]
        print(f"  ✓ Risultato {i}: {service!r:<24} distance={distance:.4f}")

    # ── Cleanup ───────────────────────────────────────────────────────────────
    deleted: int = await wipe_tenant(_TENANT)
    print(f"\nCleanup: {deleted} righe eliminate (tenant={_TENANT!r}).")

    await close_pool()
    print("\n✓ Smoke test completato con successo.\n")


if __name__ == "__main__":
    asyncio.run(main())
