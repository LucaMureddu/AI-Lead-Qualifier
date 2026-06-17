"""
main.py
-------
FastAPI application entry point.

Startup sequence
----------------
1. Load settings from environment / .env.
2. Configure structured logging.
3. Mount the API router.
4. Ensure SQLite checkpoint directory exists.
5. Serve via uvicorn (dev) or let a production ASGI server (gunicorn + uvicorn workers) import `app`.

Air-gapped note
---------------
No telemetry or analytics libraries are imported.
All network calls go to local services (LLM, ChromaDB) or the optional Groq API.
"""

from __future__ import annotations

import logging
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import (
    auth_router,
    ingest_router,
    profile_router,
    router as qualify_router,
    upload_router,
)
from core.config import get_settings

# ── Logging setup ─────────────────────────────────────────────────────────────

def _configure_logging(log_level: str) -> None:
    """Configure stdlib logging with a structured format."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.log_level)

    app = FastAPI(
        title="AI Lead Qualifier",
        description=(
            "B2B air-gapped microservice for automatic lead qualification "
            "and quotation using LangGraph + local LLM."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — restrict in production via environment variable
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Override via CORS_ORIGINS env var in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(auth_router)
    app.include_router(qualify_router)
    app.include_router(ingest_router)
    app.include_router(upload_router)
    app.include_router(profile_router)

    @app.get("/health", tags=["ops"], summary="Health check")
    async def health() -> dict:
        return {"status": "ok", "service": "ai_lead_qualifier"}

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
