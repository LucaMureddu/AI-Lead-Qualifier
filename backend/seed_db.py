"""
seed_db.py — V2
---------------
Seed script: popola la tabella ``catalogue_items`` con servizi di esempio.

Pre-requisiti
-------------
Lo schema deve esistere prima di eseguire questo script.
Eseguire le migrazioni Alembic **prima** del seed::

    cd backend/
    alembic upgrade head    ← crea l'estensione vector e la tabella
    python seed_db.py       ← inserisce i dati di esempio

Anti-pattern: questo script NON crea né altera tabelle (zero DDL).
Alembic è l'unica fonte di verità per lo schema.

Embedding
---------
I vettori qui sono zero-vector (stub). Sostituirli con embedding reali
prodotti dallo stesso modello usato da agents/mapper._embed() per garantire
la consistenza dello spazio vettoriale.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg


# ── Dati di seed ──────────────────────────────────────────────────────────────

_TENANT_ID: str = "default"

_SERVICES: list[dict] = [
    {
        "service": "Web Development",
        "price": 1500.0,
        "description": "Sviluppo siti web e landing page aziendali.",
    },
    {
        "service": "Email Server Setup",
        "price": 300.0,
        "description": "Configurazione server di posta e protocolli email.",
    },
    {
        "service": "Cloud Hosting",
        "price": 800.0,
        "description": "Hosting su infrastruttura cloud con SLA garantito.",
    },
    {
        "service": "IT Consulting",
        "price": 120.0,
        "description": "Consulenza IT oraria per PMI e startup.",
    },
]


# ── Seed ──────────────────────────────────────────────────────────────────────

async def seed() -> None:
    dsn: str = os.getenv(
        "DATABASE_DSN",
        "postgresql://app:password@localhost/ai_lead_qualifier",
    )
    embedding_dim: int = int(os.getenv("PGVECTOR_EMBEDDING_DIM", "768"))

    conn: asyncpg.Connection = await asyncpg.connect(dsn)
    try:
        # Verifica che la tabella esista (crea un errore chiaro se Alembic
        # non è stato eseguito, invece di un generico "relation not found").
        table_exists: bool = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public'
                AND   table_name   = 'catalogue_items'
            )
            """
        )
        if not table_exists:
            raise RuntimeError(
                "Tabella 'catalogue_items' non trovata. "
                "Eseguire prima: cd backend/ && alembic upgrade head"
            )

        stub_embedding: list[float] = [0.0] * embedding_dim  # TODO: embedding reale

        inserted: int = 0
        for svc in _SERVICES:
            await conn.execute(
                """
                INSERT INTO catalogue_items
                    (tenant_id, service, price, description, embedding)
                VALUES ($1, $2, $3, $4, $5::vector)
                ON CONFLICT (tenant_id, service)
                DO UPDATE SET
                    price       = EXCLUDED.price,
                    description = EXCLUDED.description
                """,
                _TENANT_ID,
                svc["service"],
                svc["price"],
                svc["description"],
                stub_embedding,
            )
            inserted += 1

        count: int = await conn.fetchval(
            "SELECT COUNT(*) FROM catalogue_items WHERE tenant_id = $1",
            _TENANT_ID,
        )
        print(
            f"Seed completato: {inserted} servizi scritti, "
            f"{count} righe totali per tenant='{_TENANT_ID}'."
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed())
