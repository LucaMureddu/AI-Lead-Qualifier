"""
tests/integration/vector_store/conftest.py
-------------------------------------------
Fixtures per i test di integrazione pgvector.

Architettura
------------
1. ``pg_container`` (scope=session, sincrono)
   Avvia un container Docker ``pgvector/pgvector:pg16`` una sola volta
   per l'intera sessione di test. Nessun mock, nessun database locale richiesto.

2. ``asyncpg_dsn`` (scope=session, sincrono)
   Estrae il DSN asyncpg dal container ed esegue le migrazioni Alembic
   via Python API (stessa DDL di produzione → test end-to-end del DDL).

3. ``pg_pool`` (scope=function, asincrono)
   Crea un pool asyncpg per ogni singolo test e inietta il singleton
   ``database.db_core._pool`` così che ``vector_store.py`` usi il pool
   del container senza modifiche al codice applicativo.
   Il pool viene chiuso e il singleton ripristinato nel teardown.

4. ``clean_catalogue`` (scope=function, asincrono, autouse=True)
   Esegue ``TRUNCATE TABLE catalogue_items`` dopo ogni test per garantire
   l'isolamento tra i test senza dover ricreare la tabella.

Contratto di sicurezza dei log
-------------------------------
I fixture non loggano mai vettori completi o stringhe di testo grezzo.
Negli output di pytest vengono stampati solo: conteggio righe, tenant_id,
revision Alembic corrente.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Optional

import asyncpg
import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from testcontainers.postgres import PostgresContainer

import database.db_core as db_core_module

# ── Costanti ──────────────────────────────────────────────────────────────────

PGVECTOR_IMAGE: str = "pgvector/pgvector:pg16"

# backend/ directory — radice di alembic.ini e dello script_location
BACKEND_DIR: Path = Path(__file__).resolve().parent.parent.parent.parent


# ── 1. Container ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def pg_container() -> Generator[PostgresContainer, None, None]:
    """
    Avvia un container Postgres+pgvector una volta per l'intera sessione.

    Teardown automatico via context manager di testcontainers.
    """
    with PostgresContainer(
        image=PGVECTOR_IMAGE,
        dbname="test_db",
        username="test",
        password="test",
    ) as container:
        yield container


# ── 2. DSN + migrazioni ───────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def asyncpg_dsn(pg_container: PostgresContainer) -> str:
    """
    Ritorna il DSN asyncpg del container e applica le migrazioni Alembic.

    Le migrazioni vengono eseguite tramite l'API Python di Alembic
    (non via subprocess) per garantire che lo schema in test sia identico
    a quello di produzione — single source of truth per il DDL.

    Il DSN psycopg2 (formato SQLAlchemy) viene usato solo qui per Alembic.
    Tutto il resto del codice usa il DSN asyncpg.
    """
    # testcontainers ritorna un URL psycopg2: postgresql+psycopg2://...
    psycopg2_url: str = pg_container.get_connection_url()

    # Applica migrazioni (sincrono — fixture sincrona, nessun asyncio.run())
    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", psycopg2_url)
    alembic_command.upgrade(cfg, "head")

    # Converti in formato asyncpg per il resto dei test
    async_dsn: str = psycopg2_url.replace("postgresql+psycopg2://", "postgresql://")
    return async_dsn


# ── 3. Pool per test + iniezione singleton ────────────────────────────────────

@pytest_asyncio.fixture
async def pg_pool(asyncpg_dsn: str) -> AsyncGenerator[asyncpg.Pool, None]:
    """
    Pool asyncpg function-scoped: compatibile con asyncio_mode="auto"
    e asyncio_default_fixture_loop_scope="function" di pyproject.toml.

    Il pool viene creato nel loop dell'event loop del test corrente,
    evitando problemi di cross-loop che affliggono i pool session-scoped.

    Iniezione singleton
    -------------------
    Sovrascrive ``database.db_core._pool`` con il pool del container.
    Le funzioni in ``database/vector_store.py`` chiamano ``get_pool()``
    che ritorna ``_pool`` se già inizializzato — nessuna modifica al codice
    applicativo necessaria.
    """
    pool: asyncpg.Pool = await asyncpg.create_pool(
        dsn=asyncpg_dsn,
        min_size=2,
        max_size=5,
        command_timeout=30,
    )

    # Salva il singleton originale (di solito None in contesto test)
    original_pool: Optional[asyncpg.Pool] = db_core_module._pool
    db_core_module._pool = pool

    yield pool

    # Teardown: ripristina il singleton e chiudi il pool
    db_core_module._pool = original_pool
    await pool.close()


# ── 4. Cleanup automatico tra test ───────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def clean_catalogue(pg_pool: asyncpg.Pool) -> AsyncGenerator[None, None]:
    """
    Truncate ``catalogue_items`` dopo ogni test (teardown).

    Garantisce isolamento totale: ogni test parte da una tabella vuota.
    ``pg_pool`` viene richiesto esplicitamente per assicurare che
    il singleton sia iniettato prima che il test body esegua.

    Nota: TRUNCATE non logga i dati eliminati — conforme al vincolo
    di sicurezza sui log (nessun vettore o testo grezzo negli output).
    """
    yield
    # Teardown — eseguito dopo il test body e dopo il teardown dei
    # fixture dipendenti da questo (es. seeded_catalogue)
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE catalogue_items")
