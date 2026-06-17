"""
core/config.py
--------------
Centralised configuration via Pydantic Settings.
All tunables come from environment variables (or a .env file).
Never hard-code paths, URLs, or secrets here.
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

    # ── OpenAI-compatible backend (Ollama, LM Studio, vLLM, …) ──────────────
    llm_base_url: str = Field(
        default="http://localhost:11434/v1",
        description=(
            "Base URL of the OpenAI-compatible inference server. "
            "Ollama default: http://localhost:11434/v1  "
            "LM Studio default: http://localhost:1234/v1"
        ),
    )
    llm_model_name: str = Field(
        default="llama3",
        description=(
            "Model identifier sent in the 'model' field of the chat/completions payload. "
            "Must match the name exposed by the inference server (e.g. 'llama3', 'mistral')."
        ),
    )

    # ── Groq cloud (used only when llm_provider='groq') ──────────────────────
    groq_api_key: str = Field(
        default="",
        description="Groq API key (used only when llm_provider='groq').",
    )
    groq_model: str = Field(
        default="llama3-8b-8192",
        description="Groq model identifier.",
    )

    # ── Legacy local GGUF (used only when llm_provider='llama') ─────────────
    llm_model_path: Path = Field(
        default=Path("models/llama-3.gguf"),
        description="Filesystem path to the GGUF model (used when llm_provider='llama').",
    )

    llm_temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    llm_max_tokens: int = Field(default=1024, gt=0)

    # ── ChromaDB ──────────────────────────────────────────────────────────────
    chroma_host: str = Field(default="localhost", description="ChromaDB server host.")
    chroma_port: int = Field(default=8001, gt=0, lt=65536)
    chroma_collection: str = Field(
        default="service_catalogue",
        description="Name of the ChromaDB collection holding the service price list.",
    )
    chroma_n_results: int = Field(
        default=3,
        description="Number of nearest-neighbour results to fetch per query.",
    )
    ingestion_chunk_size: int = Field(
        default=5,
        gt=0,
        description="Numero di righe per chunk nel normalizer. Valori bassi (5-10) per LLM locali.",
    )

    @property
    def chroma_url(self) -> str:
        return f"http://{self.chroma_host}:{self.chroma_port}"

    # ── Persistence / LangGraph ───────────────────────────────────────────────
    sqlite_db_path: Path = Field(
        default=Path("data/checkpoints.db"),
        description="Path to the SQLite file used by LangGraph SqliteSaver.",
    )

    # ── Routing thresholds ────────────────────────────────────────────────────
    max_retry_count: int = Field(
        default=2,
        description="How many times the Extractor→Mapper loop may retry before human fallback.",
    )
    mapper_min_results: int = Field(
        default=1,
        description="Minimum mapped services required to consider the Mapper successful.",
    )
    mapper_max_distance: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Se > 0, il Mapper scarta i match con distance (coseno) oltre questa "
            "soglia. 0 = disabilitato (comportamento storico: tieni sempre il match "
            "più vicino). Con la soglia attiva, una query fuori catalogo svuota "
            "mapped_services → route_after_mapper → retry → human_fallback "
            "(niente preventivi 'allucinati' su match irrilevanti)."
        ),
    )

    # ── API ───────────────────────────────────────────────────────────────────
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, gt=0, lt=65536)
    api_reload: bool = Field(default=False, description="Uvicorn auto-reload (dev only).")
    log_level: str = Field(default="INFO")

    # ── Delivery ──────────────────────────────────────────────────────────────
    delivery_max_attempts: int = Field(
        default=3,
        gt=0,
        description="Maximum number of delivery attempts before the node routes to END.",
    )
    delivery_timeout_seconds: float = Field(
        default=5.0,
        gt=0.0,
        description="HTTP request timeout (seconds) for WebhookAdapter.",
    )

    # ── PII Masking ───────────────────────────────────────────────────────────
    pii_mask_token: str = Field(
        default="[REDACTED]",
        description="Replacement string for masked PII in sanitised text.",
    )

    # ── Catalogue upload (B1) ───────────────────────────────────────────────────
    upload_dir: Path = Field(
        default=Path("uploads"),
        description="Base directory where uploaded catalogue files are stored (tenant-scoped subdirs).",
    )
    upload_max_bytes: int = Field(
        default=10 * 1024 * 1024,  # 10 MB
        gt=0,
        description="Maximum allowed size (bytes) for an uploaded catalogue file.",
    )

    # ── Tenant profile (preventivo brandizzato) ─────────────────────────────────
    profiles_dir: Path = Field(
        default=Path("data/profiles"),
        description="Directory where per-tenant company profiles are stored as JSON.",
    )
    profile_max_bytes: int = Field(
        default=2 * 1024 * 1024,  # 2 MB (include il logo in base64)
        gt=0,
        description="Maximum serialized size (bytes) of a tenant profile, logo included.",
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
