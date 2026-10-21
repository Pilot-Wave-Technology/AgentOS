"""Create the ledger's tables in a database that has none.

THE GAP THIS CLOSES. The client reads and writes `ledger_events`,
`chain_head` and `public.ledger_blobs`, and shipped no way to create any
of them. Inside agentos that was invisible -- the migration runner
had already built them -- but it made the package uninstallable in the
only sense that matters: a second repository could `pip install` it and
then do nothing with it.

IDEMPOTENT, because a test fixture calls it on every run and a consumer
may call it on every boot. Every statement in `schema.sql` carries
`IF NOT EXISTS`.

NOT A MIGRATION RUNNER. This creates the schema when it is absent; it
does not version it, diff it or alter it. A consumer that needs those
owns them, exactly as agentos does with `migrations/`. If the
shipped schema and the control panel's migrations ever disagree,
`tests/test_the_shipped_schema_matches_the_migrations.py` fails the
build rather than letting the difference reach a chain.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# INSIDE the package, not at the distribution root, because only files
# under the package directory are installed as package data -- at the root
# it would exist in the repository and be missing from every wheel. Read at
# call time rather than import time: nothing that merely imports the client
# should touch disk.
#
# `schema.sql` and `schema.py` sit side by side and mean different things:
# the .py is the event-type VOCABULARY (`task.created`, `rlagt.sim.tick`),
# the .sql is the TABLES those events are written to.
_SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def schema_sql() -> str:
    """The DDL this client needs, as text.

    Exposed separately from `init_schema` because a consumer with its own
    migration runner wants the statements, not the execution -- it can
    paste them into a numbered migration and keep one source of truth for
    how its database is built.
    """
    return _SCHEMA.read_text(encoding="utf-8")


async def init_schema(conn: Any) -> None:
    """Create the ledger tables on `conn` if they are absent.

    `conn` is anything with an awaitable `execute` -- an asyncpg
    connection in practice. Runs the whole file in one call so the
    CREATEs and their indexes arrive together; asyncpg's simple-query
    path accepts multiple statements.

    Deliberately NOT wrapped in a transaction here: a caller that wants
    atomicity owns the transaction, and one that is bootstrapping a
    throwaway test database does not care.
    """
    await conn.execute(schema_sql())
