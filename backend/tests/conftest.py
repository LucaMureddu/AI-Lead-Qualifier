"""
tests/conftest.py
-----------------
Fixture condivise per l'intera suite (vedi TESTING_PLAN.md §2.3).

Obiettivo: isolare ogni test da servizi esterni e dallo stato globale.
- get_settings() è cachata con @lru_cache → va svuotata fra i test.
- Il checkpointer LangGraph usa AsyncPostgresSaver su un container Postgres
  reale (Testcontainers), allineato con il checkpointer di produzione.
- ``make_lead_state`` costruisce un LeadState valido (con tenant_id!).
- ``api_client`` parla con l'app via httpx.ASGITransport (no rete, no porta).

LLM NON viene mai contattato: i singoli test mockano i confini
esterni (es. ``agents.extractor._call_openai_compatible`` o
``core.graph.mapper_node``).

Checkpointer Postgres (Testcontainers)
---------------------------------------
``pg_checkpointer_container`` (scope=session) avvia un container
``postgres:16`` una sola volta per l'intera sessione. Non usa pgvector
perché il checkpointer LangGraph non ne ha bisogno.

``checkpointer`` (scope=function) crea un AsyncPostgresSaver collegato al
container, chiama setup() per creare le tabelle LangGraph, e lo teardown
dopo ogni test. Ogni test riceve un checkpointer pulito (le tabelle vengono
troncate nel teardown).

Perché non AsyncSqliteSaver?
-----------------------------
AsyncPostgresSaver e AsyncSqliteSaver hanno comportamenti leggermente
diversi sulla gestione degli interrupt/resume (serializzazione msgpack vs
JSON, semantica delle transazioni). Usare lo stesso backend in test e
produzione elimina una classe di bug che altrimenti emergerebbero solo
in staging.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from typing import Callable

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from testcontainers.postgres import PostgresContainer

from api.dependencies import create_access_token
from core.config import get_settings
from core.state import AgentState, LeadContext

# Immagine Postgres standard — il checkpointer non richiede pgvector.
_POSTGRES_IMAGE = "postgres:16"


# ── Settings isolation ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch, tmp_path):
    """
    Isola le Settings per ogni test:
    - cache di get_settings() pulita prima e dopo il test;
    - provider LLM forzato a 'openai' (rotta httpx → mockabile con respx).
    - upload e profili su tmp: i test API non scrivono nel repo.
    """
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("PROFILES_DIR", str(tmp_path / "profiles"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── Container session-scoped ──────────────────────────────────────────────────

@pytest.fixture(scope="session")
def pg_checkpointer_container() -> Generator[PostgresContainer, None, None]:
    """
    Avvia un container Postgres una volta per l'intera sessione di test.

    Teardown automatico via context manager di testcontainers.
    Non serve pgvector: le tabelle LangGraph (checkpoints, checkpoint_blobs,
    checkpoint_writes, checkpoint_migrations) usano solo tipi Postgres standard.
    """
    with PostgresContainer(
        image=_POSTGRES_IMAGE,
        dbname="test_checkpoints",
        username="test",
        password="test",
    ) as container:
        yield container


# ── Checkpointer function-scoped ──────────────────────────────────────────────

@pytest_asyncio.fixture
async def checkpointer(
    pg_checkpointer_container: PostgresContainer,
) -> AsyncGenerator[AsyncPostgresSaver, None]:
    """
    AsyncPostgresSaver collegato al container Postgres di sessione.

    Per ogni test:
    - Entra nel context manager di AsyncPostgresSaver (gestisce il pool psycopg3).
    - Chiama setup() per creare/verificare le tabelle LangGraph.
    - Nel teardown tronca le tabelle checkpoint per isolare i test.

    Il container rimane in vita per tutta la sessione (scope=session);
    solo il saver e il suo pool psycopg3 vengono ricreati per ogni test.
    """
    # testcontainers restituisce un URL psycopg2; convertiamo in formato
    # psycopg3 richiesto da AsyncPostgresSaver.
    psycopg2_url: str = pg_checkpointer_container.get_connection_url()
    conn_string: str = psycopg2_url.replace("postgresql+psycopg2://", "postgresql://")

    async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
        await saver.setup()
        yield saver
        # Teardown: tronca le tabelle LangGraph per isolare i test successivi.
        # UniqueViolation su checkpoint_migrations è già gestita da setup(),
        # quindi il truncate riguarda solo i dati di checkpoint effettivi.
        async with await saver.conn.cursor() as cur:
            await cur.execute(
                "TRUNCATE TABLE checkpoint_blobs, checkpoint_writes, checkpoints RESTART IDENTITY CASCADE"
            )


@pytest.fixture
def make_lead_state() -> Callable[..., AgentState]:
    """Factory di AgentState valido (con tenant_id!). Override via kwargs."""

    def _make(raw_text: str = "Serve un sito web e un server email.", **ovr) -> AgentState:
        base: AgentState = {
            "lead": LeadContext(
                lead_id="lead-001",
                tenant_id="acme",
                raw_payload={"text": raw_text},
            ),
            "messages": [],
            "retrieved_docs": [],
            "confidence_score": 0.0,
            "human_approved": None,
            "review_feedback": None,
            "status": "queued",
            "error_detail": None,
            "sanitized_text": "",
            "extracted_services": [],
            "mapped_services": [],
            "total_quote": 0.0,
            "on_request_services": [],
            "retry_count": 0,
            "delivery_status": "PENDING",
            "delivery_attempts": 0,
            "delivery_error": None,
        }
        base.update(ovr)  # type: ignore[typeddict-item]
        return base

    return _make


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Header Authorization con un token JWT di test valido per tenant 'acme'."""
    token = create_access_token(tenant_id="acme", expires_delta_seconds=3600)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def api_client(auth_headers):
    """
    Client async che parla con l'app FastAPI senza avviare un server reale.

    Tutti i test di integrazione che usano questo client ricevono automaticamente
    l'header ``Authorization: Bearer <token>`` pre-configurato per il tenant 'acme'.
    Gli endpoint che non richiedono auth (``/health``, ``/token``) ignorano l'header.
    """
    from main import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=auth_headers,
    ) as c:
        yield c
