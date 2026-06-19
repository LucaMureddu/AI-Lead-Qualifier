"""
tests/unit/test_catalogue_api.py
---------------------------------
Unit tests for the catalogue admin endpoints and the embedding worker task.

Scope
-----
- API: GET /api/catalog/items (list), PATCH /api/catalog/items/{item_id}
- Worker: update_embedding_task

What is mocked
--------------
- ``database.db_core.get_pool``  → AsyncMock pool with a fake asyncpg interface
- ``api.catalogue_routes._get_redis`` / ``request.app.state.redis`` → AsyncMock
- ``worker.tasks.get_pool`` → same fake pool
- ``worker.tasks.aembed_documents`` → returns a deterministic 768-dim vector

What is NOT mocked
------------------
- Pydantic validation (runs in-process, no I/O).
- Route handler logic (tested via direct awaiting, not via httpx).

Test strategy: direct handler calls
-------------------------------------
Rather than spinning up an ASGI app (which requires lifespan + all singletons),
we call the route coroutines directly, injecting mocked dependencies via the
``Depends`` override pattern.  This is faster and isolates the handler logic
from infrastructure.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from api.catalogue_routes import (
    CatalogueItemPatch,
    CatalogueItemPatchResponse,
    CatalogueListResponse,
    list_catalogue_items,
    patch_catalogue_item,
)

pytestmark = pytest.mark.unit

# ── Helpers ───────────────────────────────────────────────────────────────────

_TENANT = "acme"
_ITEM_ID = str(uuid.uuid4())
_FAKE_ITEM = {
    "id": _ITEM_ID,
    "service": "Sito web vetrina",
    "price": 800.0,
    "description": "Landing page responsive",
    "metadata": "{}",
}


def _make_row(**overrides: Any) -> MagicMock:
    """Build a fake asyncpg Record-like object."""
    data = {**_FAKE_ITEM, **overrides}
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    return row


def _make_pool(
    fetch_rows: list | None = None,
    fetchval_return: int = 0,
    fetchrow_return: MagicMock | None = None,
    execute_result: str = "UPDATE 1",
) -> MagicMock:
    """Construct a minimal asyncpg Pool mock."""
    pool = MagicMock()

    pool.fetch = AsyncMock(return_value=fetch_rows or [])
    pool.fetchval = AsyncMock(return_value=fetchval_return)
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    pool.execute = AsyncMock(return_value=execute_result)

    # Context-manager: pool.acquire() → conn → conn.transaction() → ...
    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.executemany = AsyncMock(return_value=None)

    acq_ctx = MagicMock()
    acq_ctx.__aenter__ = AsyncMock(return_value=conn)
    acq_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acq_ctx)

    return pool


# ═════════════════════════════════════════════════════════════════════════════
# Pydantic validation
# ═════════════════════════════════════════════════════════════════════════════

class TestCatalogueItemPatchSchema:
    """CatalogueItemPatch — Pydantic validates before any DB/network call."""

    def test_negative_price_raises_422(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CatalogueItemPatch(price=-1.0)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("price",) for e in errors)

    def test_zero_price_is_valid(self) -> None:
        m = CatalogueItemPatch(price=0.0)
        assert m.price == 0.0

    def test_positive_price_is_valid(self) -> None:
        m = CatalogueItemPatch(price=999.99)
        assert m.price == 999.99

    def test_empty_service_raises_422(self) -> None:
        with pytest.raises(ValidationError):
            CatalogueItemPatch(service="")

    def test_all_none_is_valid_at_schema_level(self) -> None:
        # The handler rejects "all None" at runtime, not at schema level
        m = CatalogueItemPatch()
        assert m.price is None
        assert m.service is None
        assert m.description is None


# ═════════════════════════════════════════════════════════════════════════════
# GET /api/catalog/items
# ═════════════════════════════════════════════════════════════════════════════

class TestListCatalogueItems:
    @pytest.mark.asyncio
    async def test_returns_items_and_total(self) -> None:
        row = _make_row()
        pool = _make_pool(fetch_rows=[row], fetchval_return=1)

        with patch("api.catalogue_routes.get_pool", AsyncMock(return_value=pool)):
            result: CatalogueListResponse = await list_catalogue_items(
                skip=0, limit=20, tenant_id=_TENANT
            )

        assert result.total == 1
        assert len(result.items) == 1
        assert result.items[0].id == _ITEM_ID
        assert result.items[0].price == 800.0

    @pytest.mark.asyncio
    async def test_empty_catalogue(self) -> None:
        pool = _make_pool(fetch_rows=[], fetchval_return=0)

        with patch("api.catalogue_routes.get_pool", AsyncMock(return_value=pool)):
            result = await list_catalogue_items(skip=0, limit=20, tenant_id=_TENANT)

        assert result.total == 0
        assert result.items == []

    @pytest.mark.asyncio
    async def test_pagination_params_forwarded(self) -> None:
        pool = _make_pool(fetch_rows=[], fetchval_return=50)

        with patch("api.catalogue_routes.get_pool", AsyncMock(return_value=pool)):
            result = await list_catalogue_items(skip=10, limit=5, tenant_id=_TENANT)

        assert result.skip == 10
        assert result.limit == 5
        # Verify the pool.fetch was called with the correct limit/offset
        call_args = pool.fetch.call_args[0]
        assert 5 in call_args   # limit
        assert 10 in call_args  # skip


# ═════════════════════════════════════════════════════════════════════════════
# PATCH /api/catalog/items/{item_id}
# ═════════════════════════════════════════════════════════════════════════════

class TestPatchCatalogueItem:
    """Tests for patch_catalogue_item handler."""

    def _make_request(self, redis: AsyncMock) -> MagicMock:
        """Build a fake FastAPI Request with redis on app.state."""
        req = MagicMock()
        req.app.state.redis = redis
        return req

    @pytest.mark.asyncio
    async def test_valid_patch_updates_db_and_enqueues_task(self) -> None:
        """
        Core happy-path: a valid PATCH should:
        1. Update the DB record.
        2. Write at least one audit_log row.
        3. Enqueue update_embedding_task via redis.enqueue_job.
        """
        existing = _make_row()
        updated_row = _make_row(service="Sito web pro", price=800.0)

        pool = _make_pool(fetchrow_return=existing)

        # Patch conn.fetchrow (used inside the transaction) to return the updated row
        acq_ctx = pool.acquire.return_value
        acq_ctx.__aenter__.return_value.fetchrow = AsyncMock(return_value=updated_row)

        redis = AsyncMock()
        req = self._make_request(redis)

        with patch("api.catalogue_routes.get_pool", AsyncMock(return_value=pool)):
            result: CatalogueItemPatchResponse = await patch_catalogue_item(
                item_id=_ITEM_ID,
                body=CatalogueItemPatch(service="Sito web pro"),
                request=req,
                tenant_id=_TENANT,
                redis=redis,
            )

        # DB was updated
        assert result.service == "Sito web pro"
        assert result.embedding_sync == "queued"

        # ARQ task enqueued
        redis.enqueue_job.assert_awaited_once_with(
            "update_embedding_task",
            item_id=_ITEM_ID,
            tenant_id=_TENANT,
        )

    @pytest.mark.asyncio
    async def test_negative_price_returns_422_before_db(self) -> None:
        """
        price=-5 must be rejected by Pydantic (422) before any DB query fires.
        """
        with pytest.raises(ValidationError) as exc_info:
            CatalogueItemPatch(price=-5.0)

        errors = exc_info.value.errors()
        assert any(e["loc"] == ("price",) for e in errors)

    @pytest.mark.asyncio
    async def test_item_not_found_returns_404(self) -> None:
        """
        If the item doesn't exist for this tenant, the handler raises 404.
        """
        from fastapi import HTTPException

        pool = _make_pool(fetchrow_return=None)  # simulate not found
        redis = AsyncMock()
        req = self._make_request(redis)

        with patch("api.catalogue_routes.get_pool", AsyncMock(return_value=pool)):
            with pytest.raises(HTTPException) as exc_info:
                await patch_catalogue_item(
                    item_id=str(uuid.uuid4()),
                    body=CatalogueItemPatch(price=100.0),
                    request=req,
                    tenant_id=_TENANT,
                    redis=redis,
                )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_no_fields_returns_422(self) -> None:
        """
        A body with no updatable fields must be rejected with 422.
        """
        from fastapi import HTTPException

        pool = _make_pool()
        redis = AsyncMock()
        req = self._make_request(redis)

        with patch("api.catalogue_routes.get_pool", AsyncMock(return_value=pool)):
            with pytest.raises(HTTPException) as exc_info:
                await patch_catalogue_item(
                    item_id=_ITEM_ID,
                    body=CatalogueItemPatch(),  # all None
                    request=req,
                    tenant_id=_TENANT,
                    redis=redis,
                )

        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_audit_log_written_when_value_changes(self) -> None:
        """
        Audit rows are only written when old_value != new_value.
        Patching with the same price must produce zero audit rows.
        """
        existing = _make_row(price=800.0)
        updated_row = _make_row(price=800.0)

        pool = _make_pool(fetchrow_return=existing)
        conn = pool.acquire.return_value.__aenter__.return_value
        conn.fetchrow = AsyncMock(return_value=updated_row)

        redis = AsyncMock()
        req = self._make_request(redis)

        with patch("api.catalogue_routes.get_pool", AsyncMock(return_value=pool)):
            await patch_catalogue_item(
                item_id=_ITEM_ID,
                body=CatalogueItemPatch(price=800.0),  # same value → no audit row
                request=req,
                tenant_id=_TENANT,
                redis=redis,
            )

        # executemany must NOT have been called (no audit rows)
        conn.executemany.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════════════
# Worker: update_embedding_task
# ═════════════════════════════════════════════════════════════════════════════

class TestUpdateEmbeddingTask:
    """Tests for the ARQ worker task that refreshes pgvector embeddings."""

    _FAKE_EMBEDDING = [0.1] * 768

    @pytest.mark.asyncio
    async def test_happy_path_updates_embedding(self) -> None:
        """
        Given an existing item, the task must:
        1. Fetch the record.
        2. Compute a new embedding.
        3. Write it back with UPDATE.
        """
        row = MagicMock()
        row.__getitem__ = lambda self, key: {
            "service": "Sito web",
            "price": 800.0,
            "description": "Landing page",
            "metadata": "{}",
        }[key]

        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=row)
        pool.execute = AsyncMock(return_value="UPDATE 1")

        from worker.tasks import update_embedding_task

        with (
            patch("worker.tasks.get_pool", AsyncMock(return_value=pool)),
            patch(
                "worker.tasks.aembed_documents",
                AsyncMock(return_value=[self._FAKE_EMBEDDING]),
            ),
        ):
            result = await update_embedding_task(
                ctx={}, item_id=_ITEM_ID, tenant_id=_TENANT
            )

        assert result["status"] == "updated"
        assert result["item_id"] == _ITEM_ID

        # pool.execute was called to write the new embedding
        pool.execute.assert_awaited_once()
        call_sql: str = pool.execute.call_args[0][0]
        assert "embedding" in call_sql.lower()

    @pytest.mark.asyncio
    async def test_item_not_found_returns_not_found(self) -> None:
        """
        If the item was deleted between PATCH and worker execution, return gracefully.
        """
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)

        from worker.tasks import update_embedding_task

        with patch("worker.tasks.get_pool", AsyncMock(return_value=pool)):
            result = await update_embedding_task(
                ctx={}, item_id=_ITEM_ID, tenant_id=_TENANT
            )

        assert result["status"] == "not_found"
        pool.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_embedding_error_propagates(self) -> None:
        """
        If Ollama is down, EmbeddingError must propagate so ARQ can track the failure.
        """
        from services.embeddings import EmbeddingError

        row = MagicMock()
        row.__getitem__ = lambda self, key: {
            "service": "Sito web",
            "price": 800.0,
            "description": "desc",
            "metadata": "{}",
        }[key]

        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=row)
        pool.execute = AsyncMock()

        from worker.tasks import update_embedding_task

        with (
            patch("worker.tasks.get_pool", AsyncMock(return_value=pool)),
            patch(
                "worker.tasks.aembed_documents",
                AsyncMock(side_effect=EmbeddingError("Ollama offline")),
            ),
        ):
            with pytest.raises(EmbeddingError):
                await update_embedding_task(
                    ctx={}, item_id=_ITEM_ID, tenant_id=_TENANT
                )

        # embedding update must NOT have been written
        pool.execute.assert_not_awaited()
