"""
database/profiles.py
--------------------
Async Postgres helpers for tenant profile persistence — V2.

Replaces the filesystem JSON approach (data/profiles/<tenant_id>.json).
Profiles are stored in the ``tenant_profiles`` table created by migration
002_tenant_profiles.py.

All functions use the shared asyncpg pool from db_core.get_pool().
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import structlog

from database.db_core import get_pool

log = structlog.get_logger()


async def get_profile(tenant_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch the profile JSON for *tenant_id*.

    Returns None if no profile exists yet (caller should return a default).
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT profile FROM tenant_profiles WHERE tenant_id = $1",
        tenant_id,
    )
    if row is None:
        return None
    # asyncpg returns JSONB columns as str; parse to dict
    raw = row["profile"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def upsert_profile(tenant_id: str, profile: Dict[str, Any]) -> None:
    """
    Insert or update the profile JSON for *tenant_id*.

    Uses INSERT … ON CONFLICT (tenant_id) DO UPDATE so the operation is
    idempotent and concurrent-safe without application-level locking.
    """
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO tenant_profiles (tenant_id, profile, updated_at)
        VALUES ($1, $2::jsonb, now())
        ON CONFLICT (tenant_id) DO UPDATE
            SET profile    = EXCLUDED.profile,
                updated_at = EXCLUDED.updated_at
        """,
        tenant_id,
        json.dumps(profile, ensure_ascii=False),
    )
    log.info("profile.upserted", tenant_id=tenant_id)
