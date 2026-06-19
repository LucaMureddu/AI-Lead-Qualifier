"""
scripts/migrate_profiles_to_db.py
----------------------------------
One-shot utility: import existing per-tenant JSON profiles from the legacy
filesystem directory (data/profiles/) into the new tenant_profiles Postgres
table (created by migration 002_tenant_profiles).

Run this ONCE after applying migration 002, before removing the JSON files.

Usage
-----
    cd backend/
    python scripts/migrate_profiles_to_db.py [--profiles-dir data/profiles]

The script is idempotent: it uses the same UPSERT as the production code, so
running it multiple times is safe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure the backend package root is on sys.path when run as a script.
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg

from core.config import get_settings


async def main(profiles_dir: Path) -> None:
    settings = get_settings()

    if not profiles_dir.is_dir():
        print(f"[SKIP] Directory '{profiles_dir}' does not exist — nothing to migrate.")
        return

    json_files = list(profiles_dir.glob("*.json"))
    if not json_files:
        print(f"[SKIP] No .json files found in '{profiles_dir}'.")
        return

    pool = await asyncpg.create_pool(settings.database_dsn, min_size=1, max_size=3)

    migrated = 0
    errors = 0
    for path in json_files:
        tenant_id = path.stem  # filename without extension = safe tenant_id
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("tenant_id", None)  # stored separately in the PK column
            await pool.execute(
                """
                INSERT INTO tenant_profiles (tenant_id, profile, updated_at)
                VALUES ($1, $2::jsonb, now())
                ON CONFLICT (tenant_id) DO UPDATE
                    SET profile    = EXCLUDED.profile,
                        updated_at = EXCLUDED.updated_at
                """,
                tenant_id,
                json.dumps(data, ensure_ascii=False),
            )
            print(f"  [OK] {tenant_id}")
            migrated += 1
        except Exception as exc:
            print(f"  [ERROR] {tenant_id}: {exc}")
            errors += 1

    await pool.close()
    print(f"\nDone: {migrated} migrated, {errors} errors.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate legacy JSON profiles to Postgres.")
    parser.add_argument(
        "--profiles-dir",
        default="data/profiles",
        help="Path to the directory containing per-tenant .json profile files.",
    )
    args = parser.parse_args()
    asyncio.run(main(Path(args.profiles_dir)))
