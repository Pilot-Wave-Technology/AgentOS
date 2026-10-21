#!/usr/bin/env python3
"""SQL migration runner for asyncpg (no ORM).

Each migration listed in migrations/SEQUENCE is applied once, in the order
defined by that file, inside its own transaction. Already-applied migrations
are skipped via a `_migrations` tracking table that records name, sha256,
duration, and who applied it.

Order is driven by SEQUENCE, not by filesystem sort. This lets multiple
branches add migrations independently without numbering conflicts — the
order is an explicit human decision recorded in git, not inferred from
filenames. See SEQUENCE for the full rationale and rules.

The runner exits with a non-zero status on any error so Cloud Build's
`gcloud run jobs execute --wait` step can fail the build before the deploy
step runs.

Usage:
    python migrations/apply.py postgresql://user:pass@host/db
    DATABASE_URL=postgresql://... python migrations/apply.py

Before running in CI, check_sequence.py is run first (see cloudbuild files)
to validate SEQUENCE is consistent with the .sql files on disk.
"""
import asyncio
import hashlib
import os
import socket
import sys
import time

# THIS SCRIPT PRINTS NON-ASCII, AND WINDOWS CONSOLES ARE NOT UTF-8.
# The progress line is `  -> name  (sha256=...)` with a real arrow and a
# real ellipsis, and on a cp1252 console `print` raises
# UnicodeEncodeError. The migration itself had already COMMITTED by
# then, so the traceback came from the line announcing success: the
# runner exited non-zero with a database that was actually migrated,
# and the obvious next move -- run it again -- is the one thing that
# cannot help. Re-encode the stream instead of hunting the characters.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass  # not a real tty, or already utf-8; nothing to fix

import asyncpg


MIGRATIONS_DIR = os.path.dirname(__file__)
SEQUENCE_FILE = os.path.join(MIGRATIONS_DIR, "SEQUENCE")


def read_sequence() -> list[str]:
    """Read migration names from SEQUENCE in declared order.

    Skips blank lines and lines starting with #.
    Aborts if SEQUENCE is missing — it must always be present.
    """
    if not os.path.exists(SEQUENCE_FILE):
        print("FATAL: migrations/SEQUENCE not found. Cannot determine migration order.", file=sys.stderr)
        sys.exit(2)

    names = []
    with open(SEQUENCE_FILE, encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if name and not name.startswith("#"):
                names.append(name)
    return names


async def _ensure_tracking_table(conn) -> None:
    """Create _migrations + add new columns if upgrading from older schema.

    The base table existed in older deploys with just (name, applied_at).
    Adding columns is idempotent (IF NOT EXISTS) so this runner stays
    compatible with both fresh and existing databases.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS public._migrations (
            name        TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await conn.execute(
        """
        ALTER TABLE public._migrations
            ADD COLUMN IF NOT EXISTS applied_by  TEXT,
            ADD COLUMN IF NOT EXISTS duration_ms INTEGER,
            ADD COLUMN IF NOT EXISTS sha256      TEXT
        """
    )


async def _grant_app_role(conn) -> None:
    """The app's DML grants and the pod login's password -- see _access.py."""
    # Beside this file, however it was launched (script, -m, or a runner
    # whose working directory is elsewhere).
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from _access import grant_app_role, sync_leg_password
    await grant_app_role(conn)
    await sync_leg_password(conn)


async def _prime_matviews(conn) -> None:
    """Populate every materialized view once (non-concurrent). Views are created
    WITH NO DATA, so querying them — or the dashboard's REFRESH ... CONCURRENTLY —
    fails with 'has not been populated' until the first plain refresh. Two passes
    handle matviews that depend on other matviews. Idempotent + cheap on empty
    tables; runs as the migrate role (matview owner)."""
    rows = await conn.fetch(
        "SELECT matviewname FROM pg_matviews WHERE schemaname = 'public'"
    )
    names = [r["matviewname"] for r in rows]
    if not names:
        return
    for _pass in range(2):
        pending = []
        for name in names:
            try:
                await conn.execute(f'REFRESH MATERIALIZED VIEW public."{name}"')
            except Exception:
                pending.append(name)  # likely depends on another matview; retry next pass
        names = pending
        if not names:
            break
    if names:
        print(f"WARNING: could not prime materialized views: {names}", file=sys.stderr)
    else:
        print(f"Primed {len(rows)} materialized view(s).")


async def run(dsn: str) -> int:
    """Apply all pending migrations. Returns 0 on success, non-zero on failure."""
    applied_by = os.environ.get("BUILD_ID") or os.environ.get("CLOUD_RUN_JOB") \
        or os.environ.get("USER") or socket.gethostname() or "unknown"

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:
        print(f"FATAL: could not connect to database: {exc}", file=sys.stderr)
        return 2

    try:
        await _ensure_tracking_table(conn)

        applied = {
            r["name"]
            for r in await conn.fetch("SELECT name FROM public._migrations")
        }

        # Build ordered file list from SEQUENCE, not filesystem sort.
        # SEQUENCE is the single source of truth for migration order.
        sequence = read_sequence()
        pending = []
        for name in sequence:
            if name in applied:
                continue
            sql_path = os.path.join(MIGRATIONS_DIR, f"{name}.sql")
            py_path = os.path.join(MIGRATIONS_DIR, f"{name}.py")
            if os.path.exists(sql_path):
                pending.append((name, sql_path, "sql"))
            elif os.path.exists(py_path):
                pending.append((name, py_path, "py"))
            else:
                print(
                    f"FATAL: SEQUENCE references '{name}' but neither "
                    f"{name}.sql nor {name}.py found.",
                    file=sys.stderr,
                )
                return 1

        if not pending:
            await _grant_app_role(conn)
            await _prime_matviews(conn)
            print(f"No pending migrations. ({len(applied)} already applied.)")
            return 0

        print(f"Applying {len(pending)} migration(s) as {applied_by!r}:")

        for name, path, migration_type in pending:
            with open(path, "rb") as fh:
                raw_bytes = fh.read()
            sha = hashlib.sha256(raw_bytes).hexdigest()

            print(f"  → {name} [{migration_type}]  (sha256={sha[:12]}…)")
            t0 = time.monotonic()
            try:
                async with conn.transaction():
                    # A pg_dump baseline (0001) runs set_config('search_path','',false)
                    # which clears the path for the whole session. Reset it per
                    # migration so unqualified names in later migrations (e.g. the
                    # 0002 seeder) resolve against public on a fresh database.
                    await conn.execute('SET search_path TO public, "$user"')
                    if migration_type == "sql":
                        await conn.execute(raw_bytes.decode("utf-8"))
                    else:
                        import importlib.util
                        spec = importlib.util.spec_from_file_location("_migration", path)
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        await mod.up(conn)
                    await conn.execute(
                        """
                        INSERT INTO public._migrations (name, applied_by, duration_ms, sha256)
                        VALUES ($1, $2, $3, $4)
                        """,
                        name,
                        applied_by,
                        int((time.monotonic() - t0) * 1000),
                        sha,
                    )
            except Exception as exc:
                duration_ms = int((time.monotonic() - t0) * 1000)
                print(
                    f"  ✗ FAILED {name} after {duration_ms} ms: {exc}",
                    file=sys.stderr,
                )
                return 1

            duration_ms = int((time.monotonic() - t0) * 1000)
            print(f"    ok in {duration_ms} ms")

        await _grant_app_role(conn)
        await _prime_matviews(conn)
        print(f"All migrations applied. ({len(pending)} new, {len(applied)} pre-existing.)")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    dsn = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("Usage: python migrations/apply.py <DATABASE_URL>", file=sys.stderr)
        sys.exit(1)
    sys.exit(asyncio.run(run(dsn)))
