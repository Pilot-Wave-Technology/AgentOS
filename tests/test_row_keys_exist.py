"""Every `row["key"]` names a column its query actually returns.

THE OTHER HALF OF THE PREPARE SWEEP. That test proves a query's
columns exist in the schema. This one proves the CONSUMER and the
query agree -- the gap that makes renaming a column expensive.

Concretely: rename `created_at` to `timestamp`, alias it back in the
query, and the sweep stays green while every `row["created_at"]` keeps
working. Forget the alias in one of the 3,935 row reads and nothing
tells you until that branch runs. Postgres reports a prepared
statement's output columns, so the comparison is exact rather than
heuristic: no guessing what a query returns.

SCOPE is whatever `sql_row_keys` can follow -- a row subscripted in
the function that fetched it. Rows handed to another function are not
followed. That under-reports on purpose; see that module.

`asyncio.run`, not `@pytest.mark.asyncio`: pytest-asyncio is not
installed and an async test without it ERRORS rather than skips.
"""
from __future__ import annotations

import asyncio
import os
import pathlib

import pytest

import db_or_skip

import sql_row_keys

asyncpg = pytest.importorskip("asyncpg")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ROOTS = [_ROOT / "app", _ROOT / "kernel"]


def _dsn() -> str | None:
    return (os.environ.get("INTERLOCK_TEST_DSN")
            or os.environ.get("DATABASE_URL") or None)


def _wants_kernel_schema(path: str) -> bool:
    """Same rule as the PREPARE sweep: kernel SQL needs the kernel path."""
    posix = pathlib.Path(path).as_posix()
    if "/kernel/" in posix or posix.startswith("kernel/"):
        return True
    source = pathlib.Path(path).read_text(encoding="utf-8")
    return "kernel_conn" in source or "search_path" in source


async def _columns_for(conn, sql: str) -> set[str] | None:
    """Output column names, or None when the query cannot be prepared.

    A query that will not prepare is not this test's business -- the
    PREPARE sweep owns that and reports it with a better message.
    """
    try:
        stmt = await conn.prepare(sql, timeout=20)
    except Exception:                                       # noqa: BLE001
        return None
    return {a.name for a in stmt.get_attributes()}


async def _sweep(dsn: str, reads) -> list[str]:
    plain, kern = await db_or_skip.connect_pair(asyncpg)
    cache: dict[tuple[str, bool], set[str] | None] = {}
    failures: list[str] = []
    try:
        for path, line, sql, key in reads:
            kernelish = _wants_kernel_schema(path)
            cache_key = (sql, kernelish)
            if cache_key not in cache:
                cache[cache_key] = await _columns_for(
                    kern if kernelish else plain, sql)
            columns = cache[cache_key]
            if columns is None or key in columns:
                continue
            rel = pathlib.Path(path).relative_to(_ROOT).as_posix()
            failures.append(
                f'{rel}:{line}  row["{key}"] is not returned by its query\n'
                f"    returns: {', '.join(sorted(columns)) or '(nothing)'}\n"
                f"    query:   {' '.join(sql.split())[:130]}")
    finally:
        await plain.close()
        await kern.close()
    return failures


def test_every_row_key_is_a_column_the_query_returns() -> None:
    dsn = _dsn()
    if not dsn:
        pytest.skip("no INTERLOCK_TEST_DSN / DATABASE_URL")

    reads = sql_row_keys.walk(_ROOTS)
    # FAIL, NOT PASS, on a thin collection. A dataflow walk that stops
    # following rows reports no failures and looks identical to a clean
    # tree -- the trapdoor every guard in this repo has to be held over.
    assert len(reads) > 2500, (
        f"only {len(reads)} row reads were linked to a query; the walk "
        "has stopped following rows rather than the code having stopped "
        "reading them")

    failures = asyncio.run(_sweep(dsn, reads))
    assert not failures, (
        f"{len(failures)} row read(s) name a column their query does not "
        "return:\n\n" + "\n".join(failures))
