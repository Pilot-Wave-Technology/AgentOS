"""Every SQL literal is PREPAREd against a schema built from SEQUENCE.

THIS IS THE TYPE CHECKER THIS CODEBASE GAVE UP by writing raw SQL
instead of using an ORM, bought back for the price of one test.
Postgres resolves table names, column names and functions at PREPARE
time, so a statement that prepares against the real schema cannot be
referring to a table or column that does not exist. Rename a column in
a migration, miss one of the call sites, and this fails on the call
site -- in CI, by name -- instead of at 3am inside a background loop.

The schema comes from `conftest`, which applies `migrations/apply.py`
over SEQUENCE into a template database. That is the whole reason this
works: the thing being checked against is the real migration output,
not a hand-maintained model.

WHICH CONNECTION. The kernel's SQL speaks unqualified names against the
`kernel` schema and the control panel's does not, so the two get
different connections -- and that makes this the schema-boundary guard
as well. A bare `executions` in a control-plane module fails here,
which is a thing no grep-based rule could tell from a correct one.

WHAT THIS DOES NOT CATCH: semantics. A query can prepare perfectly and
still be the wrong query -- joining the wrong way, filtering on the
wrong column, returning the right shape for the wrong row. The cards
that went to the wrong thread were valid SQL. Tests still carry that
load; this only removes the schema-drift class.

`asyncio.run`, not `@pytest.mark.asyncio`: pytest-asyncio is not
installed here and an async test without it ERRORS rather than skips.
"""
from __future__ import annotations

import asyncio
import os
import pathlib

import pytest

import db_or_skip

# Plain sibling import, NOT `from tests import ...`: `tests/` has no
# __init__.py, and importing through it synthesises a namespace
# package that shadows packages/agentos-ledger-client/tests/ and
# breaks collection of the whole suite.
import sql_literals

asyncpg = pytest.importorskip("asyncpg")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ROOTS = [_ROOT / "app", _ROOT / "kernel"]

# ONLY THE "THAT THING DOES NOT EXIST" ERRORS, which is the whole point
# of the sweep and the one class nothing else here catches.
#
# Two kinds are deliberately NOT failures:
#
#   * Parameter types that cannot be inferred standing alone. `SELECT
#     $1` is legal in context; prepared bare it is indeterminate. Not a
#     defect.
#   * SYNTAX errors. A query assembled at runtime from two literals --
#     `"... WHERE q.kind = '"` + a constant -- is, on its own, a syntax
#     error, and at PREPARE time a fragment is indistinguishable from
#     genuinely malformed SQL. Including syntax would mean marking
#     dozens of legitimate fragments to buy a check that is already
#     covered: malformed SQL fails the first time any test executes it,
#     loudly. Undefined columns do not -- they fail only on the branch
#     that runs them, which is exactly the gap this test fills.
_REAL = (
    "UndefinedTableError", "UndefinedColumnError", "UndefinedFunctionError",
    "InvalidSchemaNameError", "UndefinedObjectError",
)


def _dsn() -> str | None:
    return (os.environ.get("INTERLOCK_TEST_DSN")
            or os.environ.get("DATABASE_URL") or None)


def _wants_kernel_schema(path: pathlib.Path, source: str) -> bool:
    """True when this module's SQL runs on a kernel-search_path connection.

    Everything under `kernel/` does. So does a control-plane module that
    reaches for `kernel_conn` or sets the path itself -- `kernel_decide`
    and `story_landing` both write unqualified kernel names on purpose.
    """
    if "kernel" in path.parts[:-1]:
        return True
    return "kernel_conn" in source or "search_path" in source


async def _sweep(dsn: str) -> list[str]:
    plain, kern = await db_or_skip.connect_pair(asyncpg)
    failures: list[str] = []
    try:
        sources: dict[pathlib.Path, str] = {}
        for path, line, sql in sql_literals.walk(_ROOTS):
            if path not in sources:
                sources[path] = path.read_text(encoding="utf-8")
            conn = kern if _wants_kernel_schema(path, sources[path]) else plain
            try:
                stmt = await conn.prepare(sql, timeout=20)
            except Exception as exc:                            # noqa: BLE001
                if type(exc).__name__ not in _REAL:
                    continue
                rel = path.relative_to(_ROOT).as_posix()
                failures.append(
                    f"{rel}:{line}  {type(exc).__name__}: {exc}\n"
                    f"    {' '.join(sql.split())[:160]}")
            else:
                del stmt
    finally:
        await plain.close()
        await kern.close()
    return failures


def test_every_query_prepares() -> None:
    dsn = _dsn()
    if not dsn:
        pytest.skip("no INTERLOCK_TEST_DSN / DATABASE_URL")

    found = sql_literals.walk(_ROOTS)
    # FAIL, NOT SKIP. If the AST walk breaks -- a rename, a refactor, a
    # changed layout -- it collects nothing and every query "passes".
    # That is the shape of failure this repo keeps producing: a question
    # that could not fail visibly answering "no".
    assert len(found) > 2000, (
        f"the collector found only {len(found)} SQL literals; it has "
        "stopped seeing the tree rather than the tree having got smaller")

    failures = asyncio.run(_sweep(dsn))
    assert not failures, (
        f"{len(failures)} SQL literal(s) do not prepare against the "
        f"schema built from SEQUENCE:\n\n" + "\n".join(failures))
