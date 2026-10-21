"""Which row keys a function reads, and which query produced the row.

The PREPARE sweep proves a query's COLUMNS exist. It says nothing about
the 10,114 `row["col"]` reads on the other side: alias a renamed column
and the query passes while the consumer raises KeyError on the branch
that runs it. This module supplies the missing half -- the link from a
`fetch`/`fetchrow` call to the subscripts taken on its result.

ORDERED AND BLOCK-SCOPED, which the first version was not. A function
routinely reuses the name `row` across several loops:

    for row in await conn.fetch(SQL_A):   # row is an A row here
        row["title"]
    for row in await conn.fetch(SQL_B):   # and a B row here
        row["state"]

Walking the tree and keeping one binding per name attributed every
read to whichever loop happened to be visited last, which produced 158
confident, wrong findings on the first run. Bindings are therefore
tracked in statement order, and a `for` target is bound only inside
that loop's body.

DELIBERATELY SHALLOW ELSEWHERE. A row handed to another function,
stored on self, or turned into a `dict()` is dropped rather than
guessed at. Branches get a copy of the bindings and do not export them.
That under-reports, which is the right direction: this guard only has
to be right to be worth having.

KEYS WRITTEN ONTO A ROW ARE NOT READS OF IT. `_mark_waiting` does
`r["waiting"] = ...` and reads it back. Every key assigned anywhere in
the function is allowed, collected before any read is judged.
"""
from __future__ import annotations

import ast
import pathlib

import sql_literals

# asyncpg's row-returning calls. `fetchval` returns a scalar and
# `execute` a status string -- neither is subscripted by name.
_FETCHERS = {"fetch", "fetchrow"}

_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
_BLOCKS = (ast.If, ast.While, ast.With, ast.AsyncWith, ast.Try)

Read = tuple[str, int, str]          # (sql, line, key)


def _fetch_sql(value: ast.AST | None) -> str | None:
    """The SQL literal of an `await x.fetch(...)`, or None."""
    if value is None:
        return None
    inner = value.value if isinstance(value, ast.Await) else value
    if not isinstance(inner, ast.Call):
        return None
    if not isinstance(inner.func, ast.Attribute):
        return None
    if inner.func.attr not in _FETCHERS or not inner.args:
        return None
    first = inner.args[0]
    if (isinstance(first, ast.Constant) and isinstance(first.value, str)
            and sql_literals.is_sql(first.value)):
        return first.value
    return None


def _sql_of(node: ast.AST | None, bound: dict[str, str]) -> str | None:
    """SQL behind an iterable: a bound name, or a fetch call inline."""
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    return _fetch_sql(node)


def _key(node: ast.Subscript) -> str | None:
    sl = node.slice
    return sl.value if (isinstance(sl, ast.Constant)
                        and isinstance(sl.value, str)) else None


def _stores(fn: ast.AST) -> set[str]:
    """Every key assigned onto any subscript in the function."""
    out: set[str] = set()
    for node in ast.walk(fn):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Subscript):
                key = _key(t)
                if key:
                    out.add(key)
    return out


def _reads_in(expr: ast.AST | None, bound: dict[str, str],
              out: list[Read]) -> None:
    """Row reads inside one expression.

    RECURSIVE RATHER THAN `ast.walk`, so each comprehension gets its
    OWN scope. Flattening them shares one binding map across siblings,
    and two comprehensions over different queries in one expression --

        {"files":  {r["filename"]: r["content"] for r in files},
         "skills": {r["name"]:     r["content"] for r in skills}}

    -- then judge every `r` against whichever was visited last. That
    was the last false positive this guard produced.
    """
    if expr is None:
        return

    if isinstance(expr, _COMPREHENSIONS):
        local = dict(bound)
        for gen in expr.generators:
            # The iterable is evaluated in the scope built so far, so a
            # later generator can range over an earlier target.
            _reads_in(gen.iter, local, out)
            sql = _sql_of(gen.iter, local)
            if isinstance(gen.target, ast.Name):
                local.pop(gen.target.id, None)
                if sql:
                    local[gen.target.id] = sql
            for cond in gen.ifs:
                _reads_in(cond, local, out)
        for part in ("elt", "key", "value"):
            _reads_in(getattr(expr, part, None), local, out)
        return

    if (isinstance(expr, ast.Subscript)
            and isinstance(expr.ctx, ast.Load)
            and isinstance(expr.value, ast.Name)):
        key = _key(expr)
        sql = bound.get(expr.value.id)
        if key and sql:
            out.append((sql, expr.lineno, key))

    # EVERY child, not just `ast.expr` ones. A keyword argument is an
    # `ast.keyword`, not an expression, so filtering on expr skipped
    # the whole of `f(goal_name=row["name"])` -- several hundred reads
    # across the tree, invisible for the same reason their bug would
    # have been. Statements cannot occur inside an expression, so
    # recursing into everything here is safe.
    for child in ast.iter_child_nodes(expr):
        _reads_in(child, bound, out)


def _block(stmts: list[ast.stmt], bound: dict[str, str],
           out: list[Read]) -> None:
    """Walk a statement list IN ORDER, carrying bindings forward."""
    for stmt in stmts:
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            _reads_in(stmt.iter, bound, out)
            inner = dict(bound)
            sql = _sql_of(stmt.iter, bound)
            if isinstance(stmt.target, ast.Name):
                # Bound to this query, or shadowed by something we
                # cannot follow -- either way the old binding is gone.
                inner.pop(stmt.target.id, None)
                if sql:
                    inner[stmt.target.id] = sql
            _block(stmt.body, inner, out)
            _block(stmt.orelse, dict(bound), out)
            continue

        if isinstance(stmt, _BLOCKS):
            # `If`/`While` carry a test; `With` carries withitem
            # wrappers rather than bare expressions.
            _reads_in(getattr(stmt, "test", None), bound, out)
            for item in getattr(stmt, "items", []) or []:
                _reads_in(item.context_expr, bound, out)
            for part in ("body", "orelse", "finalbody"):
                _block(getattr(stmt, part, []) or [], dict(bound), out)
            for handler in getattr(stmt, "handlers", []) or []:
                _block(handler.body, dict(bound), out)
            continue

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue                      # its own scope; walked separately

        # Reads happen before the statement rebinds anything.
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.expr):
                _reads_in(child, bound, out)

        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                sql = _fetch_sql(stmt.value)
                target_bound = bound.pop(target.id, None)
                del target_bound
                if sql:
                    bound[target.id] = sql


def function_reads(fn: ast.AST) -> list[Read]:
    out: list[Read] = []
    _block(list(getattr(fn, "body", [])), {}, out)
    allowed = _stores(fn)
    return [r for r in out if r[2] not in allowed]


def walk(roots: list[pathlib.Path]) -> list[tuple[str, int, str, str]]:
    """Every checked row read, as (file, line, sql, key)."""
    out: list[tuple[str, int, str, str]] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if sql_literals._is_test_path(path) or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for sql, line, key in function_reads(node):
                        out.append((str(path), line, sql, key))
    return out
