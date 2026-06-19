"""Initial schema: pgvector extension + catalogue_items

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-06-18

Cosa crea questa migrazione
---------------------------
1. Estensione ``vector``                    — abilita i tipi VECTOR e gli operatori di distanza
2. Tabella ``catalogue_items``             — catalogo servizi per tenant (RAG multi-tenant)
3. UNIQUE constraint (tenant_id, service)  — necessario per gli UPSERT di ingestion
4. Indice HNSW su ``embedding``            — ricerca nearest-neighbour coseno O(log n)
5. Indice B-Tree su ``tenant_id``          — scan rapido per partizione tenant

Nota su HNSW vs ivfflat
------------------------
La versione precedente (V1 stub) suggeriva ``ivfflat``.
Questa migrazione usa **HNSW** perché:

- ``ivfflat`` richiede pre-addestramento dei centroidi (``VACUUM ANALYZE``
  + numero minimo di righe), incompatibile con ingestion continua.
- ``hnsw`` costruisce il grafo in modo incrementale, quindi inserimenti
  e update non richiedono ricalcolo. Ideale per cataloghi B2B dinamici.
- Recall equivalente o superiore a parità di ``ef`` (parametro di query).

Parametri HNSW scelti
---------------------
- ``m = 16``             — connessioni per nodo; compromesso memoria/recall
- ``ef_construction = 64`` — lista candidati durante la costruzione;
                             valori più alti = grafo migliore ma build più lenta

Per modificare la dimensione dell'embedding (default 768),
cambiare ``EMBEDDING_DIM`` qui **e** in ``core/config.py::pgvector_embedding_dim``
**prima** di eseguire questa migrazione.
"""

from __future__ import annotations

from alembic import op

# ── Revision identifiers ──────────────────────────────────────────────────────

revision: str = "001_initial_schema"
down_revision: str | None = None
branch_labels = None
depends_on = None

# ── Constants ─────────────────────────────────────────────────────────────────

# Deve corrispondere a core/config.py → Settings.pgvector_embedding_dim
EMBEDDING_DIM: int = 768


# ── Migration ─────────────────────────────────────────────────────────────────

def upgrade() -> None:
    # ── 1. Estensione pgvector ─────────────────────────────────────────────────
    # IF NOT EXISTS rende la migrazione rieseguibile (idempotente su estensioni).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── 2. Tabella applicativa ────────────────────────────────────────────────
    op.execute(
        f"""
        CREATE TABLE catalogue_items (
            id          UUID  PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT  NOT NULL,
            service     TEXT  NOT NULL,
            price       FLOAT NOT NULL,
            description TEXT,
            embedding   VECTOR({EMBEDDING_DIM}),
            metadata    JSONB NOT NULL DEFAULT '{{}}'::jsonb
        )
        """
    )

    # ── 3. Vincolo di unicità (tenant_id, service) ────────────────────────────
    # Richiesto da: INSERT ... ON CONFLICT (tenant_id, service) DO UPDATE
    # in database/vector_store.py::upsert_items
    op.execute(
        """
        ALTER TABLE catalogue_items
            ADD CONSTRAINT catalogue_items_tenant_service_key
            UNIQUE (tenant_id, service)
        """
    )

    # ── 4. Indice HNSW — cosine distance ─────────────────────────────────────
    # vector_cosine_ops → operatore <=> (distanza coseno)
    # Alternativa disponibile: vector_l2_ops (distanza euclidea, operatore <->)
    op.execute(
        """
        CREATE INDEX catalogue_items_embedding_hnsw_idx
        ON  catalogue_items
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )

    # ── 5. Indice B-Tree su tenant_id ─────────────────────────────────────────
    # Garantisce scan O(log n) per la clausola WHERE tenant_id = $1
    # prima del filtro vettoriale — critico per il partizionamento multi-tenant.
    op.execute(
        """
        CREATE INDEX catalogue_items_tenant_id_idx
        ON catalogue_items (tenant_id)
        """
    )


def downgrade() -> None:
    # Ordine inverso rispetto a upgrade() per rispettare le dipendenze.
    op.execute("DROP INDEX IF EXISTS catalogue_items_tenant_id_idx")
    op.execute("DROP INDEX IF EXISTS catalogue_items_embedding_hnsw_idx")
    op.execute("DROP TABLE IF EXISTS catalogue_items")
    # L'estensione 'vector' NON viene rimossa: può essere condivisa
    # con le tabelle di LangGraph (AsyncPostgresSaver) che risiedono
    # nello stesso schema. Rimuoverla richiederebbe un downgrade completo
    # orchestrato a livello di infrastruttura.
