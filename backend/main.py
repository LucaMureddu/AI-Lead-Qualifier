"""
main.py
-------
FastAPI application entry point — V2.

V2 changes vs V1
----------------
- lifespan: runs Alembic migrations, initialises asyncpg pool, AsyncPostgresSaver,
  ARQ Redis pool, and compiled LangGraph graphs on startup; closes all on shutdown.
- Structured logging via structlog (core/logging_setup.py).
- ARQ worker is a separate process (arq worker.worker_settings.WorkerSettings).
- SSE removed from qualification flow.

Singletons stored on app.state
-------------------------------
- app.state.redis            ArqRedis pool — shared across all HTTP requests
- app.state.graph            Compiled LangGraph (qualification) — stateless, thread-safe
- app.state.ingestion_graph  Compiled LangGraph (ingestion) — idem
"""

from __future__ import annotations

import asyncio
import contextlib
import os

# LangGraph legge questa variabile all'import per decidere quali tipi custom
# sono ammessi nella deserializzazione msgpack dal checkpointer Postgres.
# Va impostata PRIMA che qualsiasi modulo langgraph venga importato.
os.environ.setdefault(
    "LANGGRAPH_ALLOWED_MSGPACK_MODULES",
    "core.state,ingestion.models",
)

import structlog
import uvicorn
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from arq.connections import RedisSettings, create_pool as arq_create_pool
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import (
    admin_router,
    auth_router,
    ingest_router,
    profile_router,
    router as qualify_router,
    upload_router,
)
from core.config import get_settings
from core.graph import build_graph, close_checkpointer, get_checkpointer
from core.logging_setup import configure_logging
from database.db_core import close_pool, get_pool
from ingestion.graph import build_ingestion_graph

log = structlog.get_logger()


# ── Alembic helper ────────────────────────────────────────────────────────────

async def _run_migrations() -> None:
    """Run pending Alembic migrations in a thread pool (non-blocking)."""
    alembic_cfg = AlembicConfig("alembic.ini")
    await asyncio.to_thread(alembic_command.upgrade, alembic_cfg, "head")
    log.info("alembic.migrations_applied")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup sequence (order matters):
      1. Configure structured logging
      2. Run Alembic migrations  ← ensures schema is up to date before any query
      3. Open asyncpg pool       ← app queries
      4. Init LangGraph checkpointer (AsyncPostgresSaver / psycopg3)
      5. Build compiled graphs   ← stateless, reused across all requests
      6. Open ARQ Redis pool     ← job enqueuing

    Shutdown: resources closed in reverse order.
    """
    settings = get_settings()
    configure_logging()

    log.info("startup.begin", host=settings.api_host, port=settings.api_port)

    # 1. Schema: apply any pending migrations before the first query
    await _run_migrations()

    # 2. asyncpg pool (app queries + vector store)
    await get_pool()
    log.info("startup.asyncpg_pool_ready", min_size=2, max_size=10)

    # 3. LangGraph checkpointer (psycopg3 pool managed by LangGraph internally)
    checkpointer = await get_checkpointer()

    # 4. Compile graphs once — they are stateless and safe to share across requests
    app.state.graph = build_graph(checkpointer=checkpointer)
    app.state.ingestion_graph = build_ingestion_graph(checkpointer)
    log.info("startup.graphs_compiled")

    # 5. ARQ Redis pool — one shared pool for all enqueue calls
    app.state.redis = await arq_create_pool(RedisSettings.from_dsn(settings.redis_dsn))
    log.info("startup.arq_pool_ready")

    yield  # ── application running ─────────────────────────────────────────

    # Shutdown: reverse order
    await app.state.redis.close()
    await close_checkpointer()
    await close_pool()
    log.info("shutdown.complete")


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="AI Lead Qualifier",
        description=(
            "B2B multi-tenant microservice for automatic lead qualification "
            "and quotation using LangGraph + pgvector + ARQ."
        ),
        version=settings.app_version,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router)
    app.include_router(qualify_router)
    app.include_router(ingest_router)
    app.include_router(upload_router)
    app.include_router(profile_router)
    app.include_router(admin_router)

    @app.get("/health", tags=["ops"], summary="Health check")
    async def health() -> JSONResponse:
        """Verifies Postgres connectivity. Returns 503 if the pool is unavailable."""
        try:
            pool = await get_pool()
            await pool.fetchval("SELECT 1")
            return JSONResponse(
                status_code=200,
                content={
                    "status": "ok",
                    "service": "ai_lead_qualifier",
                    "version": settings.app_version,
                },
            )
        except Exception as exc:
            log.error("health.check_failed", error=str(exc))
            return JSONResponse(
                status_code=503,
                content={"status": "error", "detail": str(exc)},
            )

    return app


app: FastAPI = create_app()

# ── Dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_reload,
        log_level=settings.log_level.lower(),
    )
