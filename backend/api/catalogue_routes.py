"""
api/catalogue_routes.py
-----------------------
Catalogue admin endpoints — CRUD light for the service catalogue.

Endpoints
---------
GET  /api/catalog/items              — paginated list (skip/limit)
PATCH /api/catalog/items/{item_id}  — partial update with audit trail + async re-embedding

Design decisions
----------------
- Tenant isolation is enforced on every query via WHERE tenant_id = $N.
- PATCH uses an explicit transaction: UPDATE + audit_log INSERT are atomic.
- The embedding column is NOT updated by the API: that is delegated to the
  ARQ worker via update_embedding_task (eventual consistency).
- Audit: one audit_log row per changed field, only for fields whose value
  actually changed (old_value != new_value).
- Pydantic Field(ge=0) on price prevents negative prices at the schema level,
  returning 422 Unprocessable Entity before any DB interaction.
- PATCH is rate-limited to 10/minute per IP (slowapi) to prevent ARQ queue
  flooding from burst writes. The limiter uses the Redis backend (fail-open).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import asyncpg
import structlog
from arq import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from api.dependencies import get_current_tenant_id
from core.rate_limit import limiter
from database.db_core import get_pool
from ingestion.models import PriceType

log = structlog.get_logger()

catalogue_router: APIRouter = APIRouter(
    prefix="/api/catalog",
    tags=["catalogue-admin"],
)

# ── Columns that may be patched ───────────────────────────────────────────────
_PATCHABLE_COLUMNS: frozenset[str] = frozenset({"service", "price", "description", "price_type"})

# Columns that trigger async re-embedding (price_type alone does NOT require it)
_EMBEDDING_TRIGGER_COLUMNS: frozenset[str] = frozenset({"service", "description"})


# ── Shared-resource helpers ───────────────────────────────────────────────────

def _get_redis(request: Request) -> ArqRedis:
    """Return the shared ARQ Redis pool from app.state."""
    return request.app.state.redis


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class CatalogueItemResponse(BaseModel):
    id: str
    service: str
    price: Optional[float] = None
    price_type: PriceType = PriceType.FIXED
    description: Optional[str] = None
    metadata: Dict[str, Any] = {}


class CatalogueListResponse(BaseModel):
    items: List[CatalogueItemResponse]
    total: int
    skip: int
    limit: int


class CatalogueItemPatch(BaseModel):
    """
    Partial update payload — V3.

    Tutti i campi sono opzionali (PATCH semantics).
    La coerenza price / price_type è validata da un model_validator:
      FREE     → price forzato a 0.0
      VARIABLE → price forzato a None
      FIXED    → price deve essere non-None e >= 0

    Un PATCH incoerente che supera la validazione Pydantic ma viola il CHECK
    constraint DB (es. price_type=FIXED con price=None) viene intercettato come
    asyncpg.CheckViolationError e ritornato come 422.
    """

    service: Optional[str] = Field(default=None, min_length=1)
    price: Optional[float] = Field(default=None, ge=0)
    price_type: Optional[PriceType] = None
    description: Optional[str] = None

    @model_validator(mode="after")
    def coerce_price_for_price_type(self) -> "CatalogueItemPatch":
        """
        Coercizione parziale: se il client manda solo price_type senza price,
        applichiamo le stesse regole del modello ServiceItem per evitare
        che l'UPDATE violi il CHECK constraint in DB.
        """
        if self.price_type == PriceType.FREE:
            self.price = 0.0
        elif self.price_type == PriceType.VARIABLE:
            self.price = None
        elif self.price_type == PriceType.FIXED and self.price is None:
            # FIXED richiede un prezzo esplicito — solo se l'utente sta
            # cambiando price_type senza fornire price; se price è assente
            # dal payload il DB usa il valore esistente, quindi non blochiamo.
            # Gestiamo qui solo il caso in cui entrambi siano presenti e incoerenti.
            pass
        return self


class CatalogueItemPatchResponse(BaseModel):
    id: str
    service: str
    price: Optional[float] = None
    price_type: PriceType = PriceType.FIXED
    description: Optional[str] = None
    embedding_sync: str = "queued"


# ── GET /api/catalog/items ────────────────────────────────────────────────────

@catalogue_router.get("/items", response_model=CatalogueListResponse)
async def list_catalogue_items(
    skip: int = Query(default=0, ge=0, description="Rows to skip (pagination offset)"),
    limit: int = Query(default=20, ge=1, le=100, description="Max rows to return"),
    tenant_id: str = Depends(get_current_tenant_id),
) -> CatalogueListResponse:
    """
    Return a paginated list of catalogue items for the authenticated tenant.

    The ``embedding`` column is intentionally excluded — it is a large binary
    vector (768 floats) with no value in a UI table.
    """
    pool: asyncpg.Pool = await get_pool()

    rows = await pool.fetch(
        """
        SELECT id::text, service, price, price_type, description, metadata
        FROM   catalogue_items
        WHERE  tenant_id = $1
        ORDER  BY service
        LIMIT  $2 OFFSET $3
        """,
        tenant_id,
        limit,
        skip,
    )
    total: int = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM catalogue_items WHERE tenant_id = $1",
            tenant_id,
        )
        or 0
    )

    items = [
        CatalogueItemResponse(
            id=row["id"],
            service=row["service"],
            price=row["price"],
            price_type=PriceType(row["price_type"]),
            description=row["description"],
            metadata=json.loads(row["metadata"] or "{}"),
        )
        for row in rows
    ]

    log.debug(
        "catalogue.list",
        tenant_id=tenant_id,
        total=total,
        returned=len(items),
        skip=skip,
        limit=limit,
    )
    return CatalogueListResponse(
        items=items,
        total=total,
        skip=skip,
        limit=limit,
    )


# ── PATCH /api/catalog/items/{item_id} ───────────────────────────────────────

@catalogue_router.patch("/items/{item_id}", response_model=CatalogueItemPatchResponse)
@limiter.limit("10/minute")
async def patch_catalogue_item(
    item_id: str,
    body: CatalogueItemPatch,
    request: Request,
    tenant_id: str = Depends(get_current_tenant_id),
    redis: ArqRedis = Depends(_get_redis),
) -> CatalogueItemPatchResponse:
    """
    Partially update a catalogue item.

    Only fields provided in the request body are modified (PATCH semantics).
    For every field whose value actually changes, an audit_log row is written
    atomically in the same transaction.

    After the DB transaction commits, ``update_embedding_task`` is enqueued
    on ARQ so the pgvector embedding is regenerated asynchronously (eventual
    consistency — the search may return slightly stale data until the worker runs).

    Returns 422 if:
    - ``price`` is negative (Pydantic validation).
    - No fields are provided.

    Returns 404 if:
    - The item does not exist for this tenant.
    """
    updates: Dict[str, Any] = body.model_dump(exclude_none=True)
    # model_dump(exclude_none=True) scarta price=None, ma per VARIABLE
    # dobbiamo esplicitamente azzerare il prezzo nel DB.
    if body.price_type == PriceType.VARIABLE:
        updates["price"] = None
    if not updates:
        raise HTTPException(
            status_code=422,
            detail="Almeno un campo (service, price, description) deve essere fornito.",
        )

    pool: asyncpg.Pool = await get_pool()

    # ── 1. Read current record (tenant-scoped) ────────────────────────────────
    existing = await pool.fetchrow(
        """
        SELECT id::text, service, price, price_type, description
        FROM   catalogue_items
        WHERE  id = $1::uuid AND tenant_id = $2
        """,
        item_id,
        tenant_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail=f"Item '{item_id}' non trovato per il tenant '{tenant_id}'.",
        )

    # ── 2. Build dynamic SET clause ───────────────────────────────────────────
    set_parts: list[str] = []
    params: list[Any] = []
    param_idx = 1

    for field, value in updates.items():
        if field not in _PATCHABLE_COLUMNS:
            continue
        set_parts.append(f"{field} = ${param_idx}")
        params.append(value)
        param_idx += 1

    if not set_parts:
        raise HTTPException(
            status_code=422,
            detail="Nessun campo valido fornito. Campi accettati: service, price, price_type, description.",
        )

    # WHERE parameters come last
    where_id_idx = param_idx
    where_tenant_idx = param_idx + 1
    params.extend([item_id, tenant_id])

    # ── 3. Atomic UPDATE + audit_log ─────────────────────────────────────────
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                updated = await conn.fetchrow(
                    f"""
                    UPDATE catalogue_items
                       SET {", ".join(set_parts)}
                     WHERE id = ${where_id_idx}::uuid AND tenant_id = ${where_tenant_idx}
                    RETURNING id::text, service, price, price_type, description
                    """,
                    *params,
                )
            except asyncpg.CheckViolationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Combinazione price / price_type non valida. "
                        "Invariante: FREE → price=0.0 | FIXED → price>=0 non null | "
                        f"VARIABLE → price deve essere omesso. [{exc.constraint_name}]"
                    ),
                ) from exc
            if updated is None:
                # Extremely unlikely (checked above), but guard against TOCTOU.
                raise HTTPException(status_code=404, detail="Item non trovato.")

            # Collect audit rows — only for fields that actually changed
            audit_rows: list[tuple[str, str, Optional[str], str]] = []
            for field, new_val in updates.items():
                if field not in _PATCHABLE_COLUMNS:
                    continue
                old_val = existing[field]
                if str(old_val) != str(new_val):
                    audit_rows.append(
                        (
                            item_id,
                            field,
                            str(old_val) if old_val is not None else None,
                            str(new_val),
                        )
                    )

            if audit_rows:
                await conn.executemany(
                    """
                    INSERT INTO audit_log (item_id, field_changed, old_value, new_value)
                    VALUES ($1::uuid, $2, $3, $4)
                    """,
                    audit_rows,
                )

    # ── 4. Enqueue async embedding update (solo se service o description cambiano) ──
    # price e price_type non influenzano l'embedding (calcolato su service + description).
    needs_reembedding = any(f in _EMBEDDING_TRIGGER_COLUMNS for f in updates)
    if needs_reembedding:
        await redis.enqueue_job(
            "update_embedding_task",
            item_id=item_id,
            tenant_id=tenant_id,
        )
    embedding_sync = "queued" if needs_reembedding else "not_needed"

    log.info(
        "catalogue.item_patched",
        item_id=item_id,
        tenant_id=tenant_id,
        fields=list(updates.keys()),
        audit_rows_written=len(audit_rows),
    )
    return CatalogueItemPatchResponse(
        id=updated["id"],
        service=updated["service"],
        price=updated["price"],
        price_type=PriceType(updated["price_type"]),
        description=updated["description"],
        embedding_sync=embedding_sync,
    )
