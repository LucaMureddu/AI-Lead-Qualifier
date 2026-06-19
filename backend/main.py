"""
main.py
-------
FastAPI application entry point — V2.1.

V2 changes vs V1
----------------
- lifespan: runs Alembic migrations, initialises asyncpg pool, AsyncPostgresSaver,
  ARQ Redis pool, and compiled LangGraph graphs on startup; closes all on shutdown.
- Structured logging via structlog (core/logging_setup.py).
- ARQ worker is a separate process (arq worker.worker_settings.WorkerSettings).
- SSE removed from qualification flow.

V2.1 changes vs V2
------------------
- slowapi Limiter: rate limiting su /lead e /token, backend Redis (fail-open).
  Se Redis non è raggiungibile, slowapi logga l'errore ma non blocca la request
  (fail-open) — configurato tramite RateLimitExceeded handler e on_error callback.
  Handler 429 registrato globalmente per restituire JSON invece dell'HTML di default.
- services/storage.py: sessione aioboto3 inizializzata nel lifespan e chiusa
  nello shutdown (close_storage()). Nessun filesystem locale per gli upload.
- Health check esteso: verifica anche la raggiungibilità dell'endpoint S3/MinIO.

Singletons stored on app.state
-------------------------------
- app.state.redis            ArqRedis pool — shared across all HTTP requests
- app.state.graph            Compiled LangGraph (qualification) — stateless, thread-safe
- app.state.ingestion_graph  Compiled LangGraph (ingestion) — idem
- app.state.limiter          slowapi Limiter — referenziato dai decorator di route
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
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from api.catalogue_routes import catalogue_router
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
from core.rate_limit import limiter
from database.db_core import close_pool, get_pool
from ingestion.graph import build_ingestion_graph
from services.storage import close_storage

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
      7. Init aioboto3 storage session ← S3/MinIO (lazy, first call)

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

    # 6. aioboto3 storage session — lazy singleton in services/storage.py.
    #    Nessuna connessione viene aperta qui: il client S3 è creato per ogni
    #    operazione. Log del solo endpoint, mai delle credenziali.
    log.info("startup.storage_ready", s3_endpoint=settings.s3_endpoint_url)

    yield  # ── application running ─────────────────────────────────────────

    # Shutdown: reverse order
    close_storage()
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

    # ── Rate limiting (slowapi) ───────────────────────────────────────────────
    # Il Limiter è un singleton di modulo (core/rate_limit.py) condiviso con
    # i decorator @limiter.limit in routes.py. Lo assegniamo ad app.state
    # affinché SlowAPIMiddleware lo trovi tramite request.app.state.limiter.
    app.state.limiter = limiter

    # SlowAPIMiddleware intercetta RateLimitExceeded e delega all'handler 429.
    app.add_middleware(SlowAPIMiddleware)

    # Handler globale 429: restituisce JSON invece dell'HTML di default di slowapi.
    app.add_exception_handler(
        RateLimitExceeded,
        _rate_limit_exceeded_handler,  # type: ignore[arg-type]
    )

    # ── CORS ─────────────────────────────────────────────────────────────────
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
    app.include_router(catalogue_router)

    @app.get("/health", tags=["ops"], summary="Health check")
    async def health(request: Request) -> JSONResponse:
        """
        Verifies connectivity to Postgres, Redis, and S3/MinIO.

        Returns 503 if any dependency is unavailable. Redis is required for
        ARQ job enqueueing — if it is down, POST /lead accepts requests but
        cannot process them, so we must surface the failure here.
        S3 is required for catalogue uploads.
        """
        checks: dict[str, str] = {}
        failed: bool = False

        # ── Postgres ──────────────────────────────────────────────────────────
        try:
            pool = await get_pool()
            await pool.fetchval("SELECT 1")
            checks["postgres"] = "ok"
        except Exception as exc:
            log.error("health.postgres_failed", error=str(exc))
            checks["postgres"] = f"error: {exc}"
            failed = True

        # ── Redis (ARQ + slowapi) ─────────────────────────────────────────────
        try:
            redis = request.app.state.redis
            await redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            log.error("health.redis_failed", error=str(exc))
            checks["redis"] = f"error: {exc}"
            failed = True

        # ── S3 / MinIO ────────────────────────────────────────────────────────
        try:
            import aioboto3
            from botocore.exceptions import BotoCoreError, ClientError

            s3_session = aioboto3.Session(
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
            )
            async with s3_session.client(
                "s3", endpoint_url=settings.s3_endpoint_url
            ) as s3:
                await s3.head_bucket(Bucket=settings.s3_bucket_name)
            checks["s3"] = "ok"
        except (ClientError, BotoCoreError, Exception) as exc:
            log.error("health.s3_failed", error=str(exc))
            checks["s3"] = f"error: {exc}"
            failed = True

        status_code = 503 if failed else 200
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "error" if failed else "ok",
                "service": "ai_lead_qualifier",
                "version": settings.app_version,
                "checks": checks,
            },
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
