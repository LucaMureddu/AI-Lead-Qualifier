"""
migrations/env.py
-----------------
Alembic environment — V2.

Driver
------
Alembic usa **psycopg2** (sincrono) solo per eseguire le migrazioni via CLI.
Il codice applicativo a runtime continua a usare **asyncpg** (asincrono),
configurato in database/db_core.py. Le due librerie non si sovrappongono.

Risoluzione DSN
---------------
La variabile d'ambiente ``DATABASE_DSN`` contiene il DSN nel formato asyncpg::

    postgresql://user:password@host:port/dbname

Questa funzione converte automaticamente in formato SQLAlchemy+psycopg2::

    postgresql+psycopg2://user:password@host:port/dbname

Se ``DATABASE_DSN`` non è impostata, viene usato il valore di fallback
definito in ``alembic.ini`` (``sqlalchemy.url``).

Esecuzione
----------
::

    cd backend/
    alembic upgrade head
    alembic downgrade -1
"""

from __future__ import annotations

import os
import re
from logging.config import fileConfig
from typing import Optional

from alembic import context
from sqlalchemy import create_engine, pool

# ── Alembic config object ──────────────────────────────────────────────────────

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Non usiamo ORM models → target_metadata = None
# (le migrazioni usano op.execute() con SQL grezzo)
target_metadata = None


# ── DSN resolution ────────────────────────────────────────────────────────────

def _resolve_sync_url() -> str:
    """
    Ritorna il DSN SQLAlchemy+psycopg2 per Alembic.

    Priorità:
    1. Variabile d'ambiente DATABASE_DSN (formato asyncpg)
    2. ``sqlalchemy.url`` in alembic.ini (già in formato psycopg2)
    """
    env_dsn: Optional[str] = os.getenv("DATABASE_DSN")

    if env_dsn:
        # asyncpg:  postgresql://...
        # psycopg2: postgresql+psycopg2://...
        return re.sub(r"^postgresql://", "postgresql+psycopg2://", env_dsn, count=1)

    # Fallback: usa il valore in alembic.ini così com'è (già in formato psycopg2)
    fallback: Optional[str] = config.get_main_option("sqlalchemy.url")
    if fallback is None:
        raise RuntimeError(
            "DATABASE_DSN env var non impostata e sqlalchemy.url assente in alembic.ini."
        )
    return fallback


# ── Offline mode ──────────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """
    Genera SQL su stdout senza connessione al DB (dry-run).

    Utile per review del DDL prima del deploy::

        alembic upgrade head --sql > migration.sql
    """
    url: str = _resolve_sync_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online mode ───────────────────────────────────────────────────────────────

def run_migrations_online() -> None:
    """Applica le migrazioni su una connessione Postgres live."""
    url: str = _resolve_sync_url()
    connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


# ── Entry point ───────────────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
