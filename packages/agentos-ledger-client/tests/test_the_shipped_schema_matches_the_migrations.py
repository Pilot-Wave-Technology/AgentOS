"""The schema the package ships must not drift from the one the control
panel runs.

WHY THIS IS A BUILD FAILURE AND NOT A NOTE. `schema.sql` is a COPY of
two migrations, made so a consumer can create the tables without
inheriting agentos's migration runner. A copy drifts. For an
ordinary table that is a nuisance; for a hash-chained one it is not —
`chain_hash` covers `previous_hash + payload`, so a column added on one
side and not the other produces rows that verify in one database and
not the other, and the symptom surfaces as "the chain is broken" rather
than "the schema differs".

WHAT IT COMPARES: column names and declared types per table, plus the
CHECK constraints that bound `actor_type`, `classification` and
`storage_backend` — those three are the ones a consumer writes against
and the ones that reject a row at INSERT time.

WHAT IT DOES NOT COMPARE: whitespace, comment text, `IF NOT EXISTS`
(added here deliberately so `init_schema` is idempotent), the
`agentos_worker` GRANT (the control plane's role, not a consumer's), and
index names. Pinning those would make the guard fail on formatting and
train people to edit it rather than read it.

THE DIRECTION IS ONE-WAY: the migrations are the source. Change them
first; this file follows.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_HERE = pathlib.Path(__file__).resolve()
_PKG = _HERE.parents[1]
# packages/agentos-ledger-client/tests → repo root
_REPO = _HERE.parents[3]

_SHIPPED = _PKG / "src" / "agentos_ledger_client" / "schema.sql"

# THE CREATE TABLE statements live in these two, and for columns that is
# the whole story -- no later migration adds or retypes a column on the
# three ledger tables.
_MIGRATIONS = (
    _REPO / "migrations" / "001_initial_schema.sql",
    _REPO / "migrations" / "ledger_blobs.sql",
)

# BUT A CONSTRAINT IS NOT WHERE IT WAS CREATED, AND READING ONLY THE TWO
# FILES ABOVE IS WHAT LET A REAL BUG THROUGH.
#
# `schema.sql` is a copy of those two migrations. This guard compared the
# copy against ITS OWN SOURCE and of course found them equal -- so it was
# structurally incapable of seeing a later ALTER, which is the only way a
# constraint ever changes. Two had:
#
#     014_goal_collaboration.sql   + goal_orchestrator
#     ledger_kernel_actors.sql     + executor, verifier
#
# The shipped package therefore refused three actor types that are 29% of
# the events on a live deployment, and this test was green the whole time.
# It also hardcoded `human,independent_agent,shca,system` as REQUIRED,
# pinning the stale answer as the correct one.
#
# So the effective schema is read from EVERY migration, in the order the
# runner applies them, and the last definition of a constraint wins --
# which is what Postgres does.
_SEQUENCE = _REPO / "migrations" / "SEQUENCE"

TABLES = ("ledger_events", "chain_head", "ledger_blobs")


def _create_block(sql: str, table: str) -> str | None:
    """The CREATE TABLE body for `table`, or None."""
    m = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?"
        + re.escape(table) + r"\s*\((.*?)\n\);",
        sql, re.S | re.I)
    return m.group(1) if m else None


def _columns(body: str) -> dict[str, str]:
    """`{name: declared type}` for one CREATE TABLE body.

    Takes the first token as the name and the second as the type, which
    is enough to catch an added, removed or retyped column. Skips
    table-level constraint lines, which start with a keyword rather than
    an identifier.
    """
    cols: dict[str, str] = {}
    depth = 0
    line_acc: list[str] = []
    for raw in body.split("\n"):
        line = re.sub(r"--.*$", "", raw).strip()
        if not line:
            continue
        line_acc.append(line)
        depth += line.count("(") - line.count(")")
        if depth > 0:
            continue                      # mid-expression, e.g. a CHECK
        line = " ".join(line_acc).rstrip(",")
        line_acc = []
        parts = line.split()
        if not parts:
            continue
        if parts[0].upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK",
                                "CONSTRAINT", "EXCLUDE"):
            continue
        cols[parts[0].strip('"').lower()] = parts[1].upper() if len(parts) > 1 else ""
    return cols


# The three enumerations a consumer's INSERT is validated against, and
# the column each bounds.
_ENUM_COLUMNS = ("actor_type", "classification", "storage_backend")


def _enum_values(sql: str, column: str) -> set[str] | None:
    """The value set the LAST CHECK on `column` in `sql` allows.

    BOTH SPELLINGS, and that is not pedantry: 001 writes
    `CHECK (actor_type IN ('a','b'))` and `ledger_kernel_actors.sql`
    writes `CHECK (actor_type = ANY (ARRAY['a','b']))`. The old matcher
    knew only the first, so even pointed at the right file it would have
    read nothing and reported no drift.

    Returns None when `sql` never constrains the column, which is how a
    caller tells "unconstrained" from "constrained to nothing".

    COMMENTS ARE STRIPPED FIRST, and the first version of this did not:
    `ledger_kernel_actors.sql` writes the new values under a `-- New.
    Strictly a superset ...` line INSIDE the ARRAY, and that sentence
    came back as an allowed actor type. A guard that cannot tell an
    explanation from the thing it is reading is the recurring bug in
    this file, now twice over.
    """
    sql = re.sub(r"--[^\n]*", "", sql)
    found: str | None = None
    pattern = (r"CHECK\s*\(\s*\(?\s*" + re.escape(column)
               + r"\s*(?:IN\s*\(|=\s*ANY\s*\(\s*ARRAY\s*\[)(.*?)[\]\)]")
    for m in re.finditer(pattern, sql, re.I | re.S):
        found = m.group(1)
    if found is None:
        return None
    return {v.strip().strip("'\"").split("::")[0].strip().strip("'\"").lower()
            for v in found.split(",") if v.strip()}


def _migration_files_in_order() -> list[pathlib.Path]:
    """Every migration the runner applies, in SEQUENCE order.

    SEQUENCE is the order of record -- `apply.py` reads it top to bottom
    rather than sorting the directory -- so a constraint's final form is
    the last one named here, not the last one alphabetically.
    """
    if not _SEQUENCE.is_file():
        return []
    out = []
    for raw in _SEQUENCE.read_text(encoding="utf-8").splitlines():
        name = raw.split("#", 1)[0].strip()
        if not name:
            continue
        p = _REPO / "migrations" / (name if name.endswith(".sql")
                                    else name + ".sql")
        if p.is_file():
            out.append(p)
    return out


@pytest.fixture(scope="module")
def shipped() -> str:
    assert _SHIPPED.is_file(), f"{_SHIPPED} is missing — the package ships no schema"
    return _SHIPPED.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def migrated() -> str:
    missing = [p for p in _MIGRATIONS if not p.is_file()]
    if missing:
        pytest.skip(f"not in the agentos tree: {missing}")
    return "\n".join(p.read_text(encoding="utf-8") for p in _MIGRATIONS)


@pytest.mark.parametrize("table", TABLES)
def test_the_table_is_shipped(shipped, table):
    assert _create_block(shipped, table) is not None, (
        f"`{table}` is not in schema.sql — the client writes to it, so a "
        f"consumer calling init_schema would still fail on first use")


@pytest.mark.parametrize("table", TABLES)
def test_the_columns_match_the_migration(shipped, migrated, table):
    mine = _create_block(shipped, table)
    theirs = _create_block(migrated, table)
    assert theirs is not None, f"`{table}` vanished from the migrations"
    a, b = _columns(mine), _columns(theirs)

    assert set(a) == set(b), (
        f"`{table}` columns drifted.\n"
        f"  only in schema.sql : {sorted(set(a) - set(b))}\n"
        f"  only in migrations : {sorted(set(b) - set(a))}\n"
        f"Change the migration first, then copy it across.")
    differing = {k: (a[k], b[k]) for k in a if a[k] != b[k]}
    assert not differing, f"`{table}` column types drifted: {differing}"


@pytest.fixture(scope="module")
def effective() -> str:
    """Every migration concatenated in SEQUENCE order — the schema as the
    runner leaves it, not as it started.

    In the published distribution every migration is squashed into one
    pg_dump, which holds every constraint in its FINAL form -- and other
    tables' too (`integration_attempts` has its own `actor_type` CHECK,
    and "last one wins" would read that). So there it is the ledger
    tables' own CREATE TABLE bodies, nothing else."""
    from app.services.tests.shipped_tree import IS_DISTRIBUTION, create_table

    if IS_DISTRIBUTION:
        return "\n".join(create_table(t) for t in TABLES)
    files = _migration_files_in_order()
    if not files:
        pytest.skip("not in the agentos tree: migrations/SEQUENCE absent")
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in files)


@pytest.mark.parametrize("column", _ENUM_COLUMNS)
def test_the_value_constraints_match(shipped, effective, column):
    """A mismatch here is an INSERT that works in agentos and is
    REJECTED in the consumer's database — the failure arrives in
    production, because a consumer's own tests write only the actor types
    it already knows about.

    Equality, not subset. The old assertion was `mine <= theirs`, which
    passes when the package is MISSING values the migrations allow —
    exactly the direction the bug went.
    """
    mine = _enum_values(shipped, column)
    theirs = _enum_values(effective, column)
    assert theirs is not None, (
        f"no CHECK on `{column}` anywhere in the migrations — if it was "
        f"dropped on purpose, drop it here too")
    assert mine is not None, (
        f"schema.sql no longer constrains `{column}`, but the migrations "
        f"do: {sorted(theirs)}")
    assert mine == theirs, (
        f"`{column}` CHECK drifted.\n"
        f"  the migrations allow, schema.sql refuses : {sorted(theirs - mine)}\n"
        f"  schema.sql allows, the migrations refuse : {sorted(mine - theirs)}\n"
        f"The migrations are the source. Change them first, then this file.")


def test_the_guard_reads_past_the_two_files_it_was_copied_from(effective):
    """A regression test for THIS TEST.

    The guard used to read `001_initial_schema.sql` and
    `ledger_blobs.sql` only — the two files `schema.sql` is a copy of —
    so it compared a copy to its own source. Both later widenings of
    `actor_type` were invisible to it.

    This asserts the fixture really does span the whole SEQUENCE, by
    naming a value that exists ONLY in a later migration. If someone
    narrows the fixture back, this fails and says why.
    """
    vals = _enum_values(effective, "actor_type") or set()
    assert "goal_orchestrator" in vals, (
        "`goal_orchestrator` is added by 014_goal_collaboration.sql, not by "
        "001 — if it is missing here the fixture has stopped reading the "
        "full SEQUENCE and this guard is blind again")
    assert {"executor", "verifier"} <= vals, (
        "`executor`/`verifier` are added by ledger_kernel_actors.sql — same "
        "failure as above, one migration later")


def test_it_is_idempotent_by_construction(shipped):
    """`init_schema` is called by test fixtures on every run and may be
    called on every boot."""
    creates = re.findall(r"CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX)\s+(IF\s+NOT\s+EXISTS)?",
                         shipped, re.I)
    bare = [kind for kind, guard in creates if not guard]
    assert not bare, f"{len(bare)} CREATE statement(s) lack IF NOT EXISTS: {bare}"


def test_it_needs_no_postgres_extension(shipped):
    """A consumer's bare test database must be enough. `gen_random_uuid`
    has been core since Postgres 13; anything needing CREATE EXTENSION
    would make `init_schema` a lie on a fresh database."""
    assert "CREATE EXTENSION" not in shipped.upper()


def test_it_does_not_grant_to_the_control_planes_role(shipped):
    """`ledger_blobs.sql` grants to `agentos_worker`. That role is
    agentos's; a consumer grants to whatever it runs as, and a
    GRANT to a non-existent role is a failed bootstrap."""
    # COMMENTS STRIPPED FIRST. The header explains why the GRANT is
    # absent, and naming the role there is the clearest way to say it —
    # a guard that cannot tell an explanation from the thing it warns
    # about makes the file harder to document than to get wrong.
    code = " ".join(re.sub(r"--.*$", "", ln) for ln in shipped.splitlines())
    assert "agentos_worker" not in code
    assert "GRANT" not in code.upper()
