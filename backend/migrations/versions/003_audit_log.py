"""
Audit log table: tracks every field-level change on catalogue_items.

Revision ID: 003_audit_log
Revises: 002_tenant_profiles
Create Date: 2026-06-19

Schema
------
audit_log
  id            BIGSERIAL PRIMARY KEY
  timestamp     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
  item_id       UUID         NOT NULL  — FK-style ref to catalogue_items.id (no hard FK
                                        so rows survive item deletion for audit trail)
  field_changed TEXT         NOT NULL  — e.g. 'service', 'price', 'description'
  old_value     TEXT                   — NULL on first-time inserts
  new_value     TEXT

Indexes
-------
- (item_id)    — fast lookup by item
- (timestamp DESC) — chronological audit queries
"""

from __future__ import annotations

from alembic import op

revision: str = "003_audit_log"
down_revision: str | None = "002_tenant_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id            BIGSERIAL    PRIMARY KEY,
            timestamp     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            item_id       UUID         NOT NULL,
            field_changed TEXT         NOT NULL,
            old_value     TEXT,
            new_value     TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS audit_log_item_id_idx ON audit_log (item_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS audit_log_timestamp_idx ON audit_log (timestamp DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS audit_log_timestamp_idx")
    op.execute("DROP INDEX IF EXISTS audit_log_item_id_idx")
    op.execute("DROP TABLE IF EXISTS audit_log")
