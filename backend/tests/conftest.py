"""
tests/conftest.py
-----------------
Fixture condivise per l'intera suite (vedi TESTING_PLAN.md §2.3).

Obiettivo: isolare ogni test da servizi esterni e dallo stato globale.
- get_settings() è cachata con @lru_cache → va svuotata fra i test.
- Il checkpointer LangGraph gira su SQLite ``:memory:`` (niente file su disco).
- ``make_lead_state`` costruisce un LeadState valido (con tenant_id!).
- ``api_client`` parla con l'app via httpx.ASGITransport (no rete, no porta).

LLM e ChromaDB NON vengono mai contattati: i singoli test mockano i confini
esterni (es. ``agents.extractor._call_openai_compatible`` o
``core.graph.mapper_node``).
"""

from __future__ import annotations

from typing import Callable

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from api.dependencies import create_access_token
from core.config import get_settings
from core.state import AgentState, LeadContext


@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch, tmp_path):
    """
    Isola le Settings per ogni test:
    - cache di get_settings() pulita prima e dopo il test;
    - DB SQLite su file temporaneo (mai il path di produzione);
    - provider LLM forzato a 'openai' (rotta httpx → mockabile con respx).
    """
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    # Isola anche upload e profili su tmp: i test API non devono scrivere nel repo.
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("PROFILES_DIR", str(tmp_path / "profiles"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def checkpointer():
    """AsyncSqliteSaver in-memory: supporta interrupt/resume, zero I/O su disco."""
    conn = await aiosqlite.connect(":memory:")
    saver = AsyncSqliteSaver(conn)
    await saver.setup()  # crea le tabelle SQLite richieste da LangGraph >= 0.2
    try:
        yield saver
    finally:
        await conn.close()


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
