"""
core/config.py
--------------
Centralised configuration via Pydantic Settings — V2.

V2 changes vs V1
----------------
- REMOVED: chroma_host, chroma_port, chroma_collection, chroma_n_results, chroma_url, sqlite_db_path
- ADDED:   database_dsn (asyncpg/Postgres), redis_dsn (ARQ), jwt_public_key_path,
           jwt_private_key_path, pgvector_embedding_dim, pgvector_n_results,
           cors_origins, app_version, token_endpoint_enabled
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings loaded from the environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────────────────────────────
    llm_provider: str = Field(
        default="openai",
        description=(
            "'openai' for any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, …); "
            "'groq' for the Groq cloud API; "
            "'llama' for a local GGUF model via llama-cpp-python (legacy)."
        ),
    )

    # ── OpenAI-compatible backend ─────────────────────────────────────────────
    llm_base_url: str = Field(
        default="http://localhost:11434/v1",
        description="Base URL of the OpenAI-compatible inference server.",
    )
    llm_model_name: str = Field(
        default="llama3",
        description="Model identifier sent in the chat/completions payload.",
    )

    # ── Groq cloud ────────────────────────────────────────────────────────────
    groq_api_key: str = Field(default="", description="Groq API key.")
    groq_model: str = Field(default="llama3-8b-8192", description="Groq model identifier.")

    # ── Legacy local GGUF ─────────────────────────────────────────────────────
    llm_model_path: Path = Field(
        default=Path("models/llama-3.gguf"),
        description="Filesystem path to the GGUF model (llm_provider='llama' only).",
    )

    llm_temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    llm_max_tokens: int = Field(default=1024, gt=0)

    # ── PostgreSQL (V2) ───────────────────────────────────────────────────────
    database_dsn: str = Field(
        default="postgresql://app:password@localhost/ai_lead_qualifier",
        description="DSN asyncpg for the Postgres pool (LangGraph checkpointer + pgvector).",
    )

    # ── Redis / ARQ (V2) ─────────────────────────────────────────────────────
    redis_dsn: str = Field(
        default="redis://localhost:6379",
        description="Redis URL for ARQ worker and job broker.",
    )

    # ── JWT RS256 (V2) ────────────────────────────────────────────────────────
    jwt_public_key_path: Path = Field(
        default=Path("keys/public.pem"),
        description="Path to the RSA public key for JWT RS256 validation.",
    )
    jwt_private_key_path: Path = Field(
        default=Path("keys/private.pem"),
        description="Path to the RSA private key for /token (dev/test only).",
    )

    # ── Embedding / Ollama (V2) ───────────────────────────────────────────────
    embedding_model: str = Field(
        default="nomic-embed-text",
        description=(
            "Modello Ollama per la generazione di vettori. "
            "Deve essere coerente con pgvector_embedding_dim: "
            "nomic-embed-text → 768 dim, mxbai-embed-large → 1024 dim."
        ),
    )
    embedding_base_url: str = Field(
        default="http://localhost:11434",
        description=(
            "URL base del server Ollama per il servizio di embedding. "
            "In Docker Compose usare http://host.docker.internal:11434. "
            "Non includere il path /api/...: lo aggiunge OllamaEmbeddings."
        ),
    )

    # ── pgvector (V2) ─────────────────────────────────────────────────────────
    pgvector_embedding_dim: int = Field(
        default=768,
        description=(
            "Dimensione del vettore di embedding archiviato in pgvector. "
            "Deve coincidere con l'output di embedding_model: "
            "nomic-embed-text → 768, mxbai-embed-large → 1024."
        ),
    )
    pgvector_n_results: int = Field(
        default=3,
        description="Number of nearest-neighbour results per pgvector query.",
    )

    # ── Ingestion ─────────────────────────────────────────────────────────────
    ingestion_chunk_size: int = Field(
        default=5,
        gt=0,
        description="Rows per chunk in the normalizer. Low values (5-10) for local LLMs.",
    )

    # ── Routing thresholds ────────────────────────────────────────────────────
    max_retry_count: int = Field(
        default=2,
        description="How many times Extractor→Mapper may retry before HITL fallback.",
    )
    mapper_min_results: int = Field(
        default=1,
        description="Minimum mapped services required to consider the Mapper successful.",
    )
    mapper_max_distance: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "If > 0, Mapper discards matches with cosine distance above this threshold. "
            "0 = disabled (always keep the closest match)."
        ),
    )

    # ── API ───────────────────────────────────────────────────────────────────
    app_version: str = Field(default="2.0.0", description="Application version string.")
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, gt=0, lt=65536)
    api_reload: bool = Field(default=False, description="Uvicorn auto-reload (dev only).")
    log_level: str = Field(default="INFO")

    # ── CORS ─────────────────────────────────────────────────────────────────
    # Env var: CORS_ORIGINS='["https://app.example.com","https://api.example.com"]'
    # In dev, defaults to localhost. In production, set explicitly in .env.prod.
    cors_origins: list[str] = Field(
        default=["http://localhost", "http://localhost:80", "http://localhost:8000"],
        description="Allowed CORS origins. Override in production via CORS_ORIGINS env var.",
    )

    # ── Security ──────────────────────────────────────────────────────────────
    # POST /token issues JWT tokens for any username — for dev/test only.
    # Set TOKEN_ENDPOINT_ENABLED=false in .env.prod to disable it entirely.
    token_endpoint_enabled: bool = Field(
        default=True,
        description="Enable the mock /token endpoint. MUST be false in production.",
    )

    # ── Delivery ──────────────────────────────────────────────────────────────
    delivery_max_attempts: int = Field(
        default=3,
        gt=0,
        description="Maximum delivery attempts before routing to END.",
    )
    delivery_timeout_seconds: float = Field(
        default=5.0,
        gt=0.0,
        description="HTTP request timeout (seconds) for WebhookAdapter.",
    )

    # ── LangGraph Serialization ───────────────────────────────────────────────
    langgraph_allowed_msgpack_modules: str = Field(
        default="core.state,ingestion.models",
        description="Evita il warning di serializzazione sui tipi custom in Postgres.",
        alias="LANGGRAPH_ALLOWED_MSGPACK_MODULES",
    )

    # ── PII Masking ───────────────────────────────────────────────────────────
    pii_mask_token: str = Field(
        default="[REDACTED]",
        description="Replacement string for masked PII in sanitised text.",
    )

    # ── Catalogue upload ──────────────────────────────────────────────────────
    upload_dir: Path = Field(
        default=Path("uploads"),
        description="Base directory for uploaded catalogue files (tenant-scoped subdirs).",
    )
    upload_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        gt=0,
        description="Maximum allowed size (bytes) for an uploaded catalogue file.",
    )

    # ── Tenant profile ────────────────────────────────────────────────────────
    # profiles_dir removed in V2.1: profiles are now stored in Postgres
    # (tenant_profiles table, migration 002_tenant_profiles). Filesystem JSON
    # files in data/profiles/ are no longer read or written.
    profile_max_bytes: int = Field(
        default=2 * 1024 * 1024,
        gt=0,
        description="Maximum serialized size (bytes) of a tenant profile (enforced before DB write).",
    )

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, v: str) -> str:
        allowed = {"openai", "groq", "llama"}
        if v not in allowed:
            raise ValueError(f"llm_provider must be one of {allowed}, got '{v}'")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (cached after first call)."""
    return Settings()
