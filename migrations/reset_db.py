"""Wipe the public schema and re-apply ALL migrations cleanly.

For repeatable testing on the SAME Cloud SQL instance: instead of destroying the
instance (~10 min to recreate), this drops the `public` schema and re-runs the
full migration sequence, giving a fresh DB (schema + seed) in ~1 min.

DESTRUCTIVE — deletes all data. Test/dev only; never run against production.

Run as a Cloud Run job with the DDL (migrate) DSN (see db-reset.sh):
    python migrations/reset_db.py            # uses $DATABASE_URL
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import asyncpg


async def _wipe(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        db = await conn.fetchval("SELECT current_database()")
        # Terminate other sessions so DROP SCHEMA won't block on the live app.
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            db,
        )
        # BOTH schemas. 0001 creates `kernel`; leaving it behind makes the
        # re-apply fail on "schema already exists".
        await conn.execute("DROP SCHEMA IF EXISTS kernel CASCADE;")
        await conn.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
        # USAGE, not ALL: what a fresh database gives PUBLIC. ALL would let
        # any login on the instance create objects here. Roles are
        # cluster-wide and survive, so the pods' login and its password do.
        await conn.execute("GRANT USAGE ON SCHEMA public TO PUBLIC;")
        print(f"[reset_db] wiped schemas 'kernel' and 'public' in database '{db}'")
    finally:
        await conn.close()


def main() -> None:
    dsn = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATABASE_URL", "")
    if not dsn:
        sys.exit("[reset_db] DATABASE_URL (or argv[1]) required")
    asyncio.run(_wipe(dsn))
    apply_py = os.path.join(os.path.dirname(__file__), "apply.py")
    print("[reset_db] re-applying migrations (schema + seed)...")
    sys.exit(subprocess.run([sys.executable, apply_py, dsn]).returncode)


if __name__ == "__main__":
    main()
