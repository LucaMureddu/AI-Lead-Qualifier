"""Tenant profiles: replace filesystem JSON with Postgres table

Revision ID: 002_tenant_profiles
Revises: 001_initial_schema
Create Date: 2026-06-19

Motivazione
-----------
I profili tenant erano salvati come file JSON su filesystem (data/profiles/).
Questa soluzione non funziona in deploy multi-istanza (3 server dietro Traefik):
ogni istanza vedrebbe solo i file locali — un PUT su server A non verrebbe
visto da server B o C. Spostare i profili in Postgres risolve il problema alla
radice e garantisce consistenza transazionale.

Schema scelto
-------------
Colonna ``profile`` di tipo JSONB invece di colonne separate per ogni campo
(company_name, iban, …). I vantaggi:

- Evolutività: aggiungere nuovi campi profilo non richiede ALTER TABLE.
- Semplicità: read/write come dict Python, nessun ORM necessario.
- Performance: i profili sono piccoli (< 2 MB) e letti raramente; JSONB
  è abbondantemente sufficiente senza indici GIN aggiuntivi.

Migrazione dati esistenti
--------------------------
I file JSON in ``data/profiles/`` NON vengono migrati automaticamente da
questo script (richiederebbe accesso al filesystem dal runner Alembic, che
non è garantito in tutti gli ambienti). Per importarli manualmente dopo
aver applicato questa migrazione:

    python scripts/migrate_profiles_to_db.py   # vedi script di utilità

In alternativa, i profili saranno ricreati dagli operatori via UI alla prima
modifica — il GET restituisce un profilo vuoto se non esiste nel DB.
"""

from __future__ import annotations

from alembic import op

# ── Revision identifiers ──────────────────────────────────────────────────────

revision: str = "002_tenant_profiles"
down_revision: str | None = "001_initial_schema"
branch_labels = None
depends_on = None


# ── Migration ─────────────────────────────────────────────────────────────────

def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tenant_profiles (
            tenant_id  TEXT        PRIMARY KEY,
            profile    JSONB       NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_profiles")
