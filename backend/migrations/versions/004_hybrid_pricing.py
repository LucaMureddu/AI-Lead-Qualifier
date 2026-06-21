"""
Hybrid Pricing V3 — promozione di price_type a colonna di prima classe.

Revision ID: 004_hybrid_pricing
Revises: 003_audit_log
Create Date: 2026-06-20

Schema changes
--------------
catalogue_items:
  - ADD COLUMN price_type VARCHAR(20) NOT NULL DEFAULT 'FIXED'
  - ALTER COLUMN price DROP NOT NULL          (NULL ⟺ VARIABLE)
  - ADD CONSTRAINT chk_hybrid_pricing_logic   (invariante C(price_type, price))

Invariante
----------
    C(price_type, price) ⟺
          (price_type = 'FREE'     ∧ price = 0.0)
        ∨ (price_type = 'FIXED'    ∧ price IS NOT NULL ∧ price >= 0.0)
        ∨ (price_type = 'VARIABLE' ∧ price IS NULL)

Perché NULL e non sentinel -1.0
---------------------------------
PostgreSQL SUM() ignora i NULL nativamente, quindi SUM(price) su righe FIXED/FREE
rimane corretto senza alcun filtro aggiuntivo.  Il sentinel -1.0 avrebbe invece
richiesto un WHERE price_type != 'VARIABLE' in ogni aggregato — ed un'omissione
sarebbe un bug silenzioso.

Downgrade
---------
drop constraint → SET NOT NULL (dopo backfill 0.0 sui NULL) → drop column.
La tabella è vuota in test, quindi il backfill è un no-op.
"""

from __future__ import annotations

from alembic import op

revision: str = "004_hybrid_pricing"
down_revision: str | None = "003_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Nuova colonna price_type — DEFAULT 'FIXED' protegge INSERT legacy
    op.execute(
        """
        ALTER TABLE catalogue_items
            ADD COLUMN price_type VARCHAR(20) NOT NULL DEFAULT 'FIXED'
        """
    )

    # 2. Rendere price nullable (VARIABLE ⟺ price IS NULL)
    op.execute(
        """
        ALTER TABLE catalogue_items
            ALTER COLUMN price DROP NOT NULL
        """
    )

    # 3. CHECK constraint che garantisce l'invariante
    op.execute(
        """
        ALTER TABLE catalogue_items
            ADD CONSTRAINT chk_hybrid_pricing_logic CHECK (
                (price_type = 'FREE'     AND price = 0.0)
                OR
                (price_type = 'FIXED'    AND price IS NOT NULL AND price >= 0.0)
                OR
                (price_type = 'VARIABLE' AND price IS NULL)
            )
        """
    )


def downgrade() -> None:
    # Ordine inverso: prima drop constraint, poi SET NOT NULL, poi drop column.
    op.execute(
        "ALTER TABLE catalogue_items DROP CONSTRAINT IF EXISTS chk_hybrid_pricing_logic"
    )
    # Backfill NULL → 0.0 prima di rimettere NOT NULL
    op.execute(
        "UPDATE catalogue_items SET price = 0.0 WHERE price IS NULL"
    )
    op.execute(
        "ALTER TABLE catalogue_items ALTER COLUMN price SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE catalogue_items DROP COLUMN IF EXISTS price_type"
    )
