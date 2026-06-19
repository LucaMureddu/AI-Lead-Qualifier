"""
database/db_core.py
-------------------
Shared asyncpg connection pool — V2.

Replaces the SQLite + ChromaDB dual-persistence of V1 with a single
Postgres instance (pgvector extension) that handles both LangGraph
checkpoints (via AsyncPostgresSaver) and the vector catalogue.

Usage
-----
    pool = await get_pool()           # returns the shared pool (lazy init)
    await close_pool()                # called in app lifespan shutdown

The pool is a module-level singleton: safe for async multi-task access,
re-entrant (returns the same pool on subsequent calls), and closed cleanly
on app shutdown via FastAPI's lifespan context manager.
"""

from __future__ import annotations

import asyncpg

from core.config import get_settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Return the shared asyncpg pool, initialising it on first access."""
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(
            dsn=settings.database_dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    """Close the shared pool (call from app lifespan shutdown)."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
