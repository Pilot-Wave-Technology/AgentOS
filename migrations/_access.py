"""What apply.py does after every run: the grants and the password that
migrations cannot carry.

Both run on EVERY migrate, not once, and both are idempotent:

  grant_app_role      the app login's DML on everything the migrate login
                      created, in BOTH schemas. Cloud SQL users do not share
                      table access, and a migration names tables, so a
                      grant written into one would miss the next table.
                      ALTER DEFAULT PRIVILEGES covers the tables later
                      migrations add.

  sync_leg_password   the execution pod's login (`agentos_leg`, created by
                      migration 0004) gets the password terraform wrote
                      into the `execution-db-password` secret. A migration
                      cannot read a secret, and setting it here every run
                      means the role and the secret cannot drift: rotate
                      the secret, push a build, and they agree again.

A missing role is NOT a failure. By the time these run every migration has
committed; exiting non-zero would report a failed migration that did not
fail. A test database has no app_user and no secret, and that is expected.
"""
from __future__ import annotations

import os

SCHEMAS = ("public", "kernel")
LEG_ROLE = "agentos_leg"


async def _role_exists(conn, role: str) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role))


async def grant_app_role(conn) -> None:
    role = os.environ.get("APP_DB_ROLE", "app_user")
    if not role.replace("_", "").isalnum():
        raise ValueError(f"unsafe APP_DB_ROLE: {role!r}")
    if not await _role_exists(conn, role):
        print(f"No role {role!r} in this database, so no DML grants to make. "
              f"Expected for a test database; a deployed one has the role "
              f"created by terraform. Set APP_DB_ROLE if yours differs.")
        return

    # KERNEL TOO. The control plane reads and writes kernel tables directly
    # (the conductor's loops, the board), not only through the three kernel
    # roles; the internal environments grant both schemas the same way.
    for schema in SCHEMAS:
        if not await conn.fetchval("SELECT 1 FROM pg_namespace WHERE nspname = $1", schema):
            continue
        q = f'"{schema}"'
        await conn.execute(f'GRANT USAGE ON SCHEMA {q} TO "{role}"')
        await conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {q} TO "{role}"')
        await conn.execute(f'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {q} TO "{role}"')
        await conn.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA {q} '
                           f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{role}"')
        await conn.execute(f'ALTER DEFAULT PRIVILEGES IN SCHEMA {q} '
                           f'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO "{role}"')
        print(f"Granted DML privileges on schema {schema} to {role!r}.")


async def sync_leg_password(conn) -> None:
    # Stripped: Secret Manager hands back exactly what was stored, and a
    # trailing newline here is a password nobody can type.
    password = os.environ.get("AGENTOS_LEG_PASSWORD", "").strip()
    if not await _role_exists(conn, LEG_ROLE):
        return
    if not password:
        print(f"AGENTOS_LEG_PASSWORD is not set, so {LEG_ROLE}'s password was "
              f"left as it is. Execution pods cannot connect until it matches "
              f"the execution-db-password secret.")
        return
    literal = await conn.fetchval("SELECT quote_literal($1)", password)
    await conn.execute(f"ALTER ROLE {LEG_ROLE} PASSWORD {literal}")
    print(f"Set {LEG_ROLE}'s password from execution-db-password.")
