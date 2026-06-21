"""
database/vector_store.py
------------------------
pgvector wrapper — V2 replacement for services/vector_db.py (ChromaDB).

Schema managed by Alembic (migrations/versions/001_initial_schema.py)
----------------------------------------------------------------------
Applied automatically at startup — no manual SQL needed.
The migration creates:
  - Extension ``vector``
  - Table ``catalogue_items`` with VECTOR(768) embedding column
  - UNIQUE constraint (tenant_id, service) — required for UPSERT
  - HNSW index on embedding (vector_cosine_ops) — O(log n) ANN search
  - B-Tree index on tenant_id — fast per-tenant scan

Note: the migration uses HNSW (not ivfflat) because HNSW builds incrementally
and does not require a pre-training step or minimum row count.

Security contract
-----------------
The ``tenant_id`` filter is MANDATORY in every query. There is no code path
that reads across tenant boundaries. Every function signature requires it.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import structlog
from langchain_core.documents import Document

from core.config import get_settings
from database.db_core import get_pool

log = structlog.get_logger()


async def similarity_search(
    query_embedding: list[float],
    tenant_id: str,
    n_results: int | None = None,
    max_distance: float | None = None,
) -> list[Document]:
    """
    Nearest-neighbour search in the tenant's service catalogue.

    Parameters
    ----------
    query_embedding : list[float]
        Embedding vector for the service query.
    tenant_id : str
        Tenant scope — MANDATORY, no cross-tenant access.
    n_results : int | None
        Override for pgvector_n_results setting.
    max_distance : float | None
        Discard matches with cosine distance above this value (0 = disabled).

    Returns
    -------
    list[Document]
        LangChain Documents with metadata: service, price, distance, tenant_id.
    """
    settings = get_settings()
    k = n_results or settings.pgvector_n_results
    pool: asyncpg.Pool = await get_pool()

    # pgvector: <=> = cosine distance
    query = """
        SELECT service, price, price_type, description, metadata,
               (embedding <=> $1::vector) AS distance
        FROM   catalogue_items
        WHERE  tenant_id = $2
        ORDER  BY embedding <=> $1::vector
        LIMIT  $3
    """
    rows = await pool.fetch(query, json.dumps(query_embedding), tenant_id, k)

    docs: list[Document] = []
    threshold = max_distance if (max_distance and max_distance > 0.0) else None
    for row in rows:
        distance: float = row["distance"]
        if threshold and distance > threshold:
            log.debug(
                "vector_store.match_discarded",
                service=row["service"],
                distance=round(distance, 4),
                threshold=threshold,
                tenant_id=tenant_id,
            )
            continue
        meta = json.loads(row["metadata"] or "{}")
        meta.update(
            {
                "service": row["service"],
                "price": row["price"],
                "price_type": row["price_type"],
                "distance": distance,
                "tenant_id": tenant_id,
            }
        )
        docs.append(
            Document(
                page_content=row["description"] or row["service"],
                metadata=meta,
            )
        )
    return docs


async def upsert_items(
    items: list[dict[str, Any]],
    tenant_id: str,
) -> int:
    """
    Insert or update catalogue items for a tenant.

    Each item must contain: service (str), price (float | None), price_type (str),
    description (str), embedding (list[float]). Optional: metadata (dict).

    price=None is valid for VARIABLE items — asyncpg maps None → NULL correctly.

    Returns the number of rows written.
    """
    pool: asyncpg.Pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO catalogue_items
                (tenant_id, service, price, price_type, description, embedding, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::vector, $7::jsonb)
            ON CONFLICT (tenant_id, service)
            DO UPDATE SET
                price       = EXCLUDED.price,
                price_type  = EXCLUDED.price_type,
                description = EXCLUDED.description,
                embedding   = EXCLUDED.embedding,
                metadata    = EXCLUDED.metadata
            """,
            [
                (
                    tenant_id,
                    item["service"],
                    item["price"],           # None ⟺ VARIABLE → NULL in DB
                    item["price_type"],
                    item.get("description", ""),
                    json.dumps(item["embedding"]),
                    json.dumps(item.get("metadata", {})),
                )
                for item in items
            ],
        )
    log.info("vector_store.upserted", count=len(items), tenant_id=tenant_id)
    return len(items)


async def wipe_tenant(tenant_id: str) -> int:
    """
    Delete all vector data for a tenant (hard reset).

    Returns the number of rows deleted.
    """
    pool: asyncpg.Pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM catalogue_items WHERE tenant_id = $1", tenant_id
    )
    deleted = int(result.split()[-1])
    log.warning("vector_store.wiped", tenant_id=tenant_id, rows_deleted=deleted)
    return deleted
