"""The row-key analyser has to fire, and has to not fire.

Same reasoning as `test_expand_contract_guard`: a guard is the easiest
thing in the world to write so that it never reports anything, and it
looks identical to a clean repository right up until the day it was
meant to catch something. This one reports nothing today, so the only
evidence it works is here.

Both directions matter. The analyser's two earlier versions were wrong
in opposite ways -- the first attributed every read to whichever
binding the tree walk saw last (158 confident, wrong findings), the
second shared one scope across sibling comprehensions (one more). Each
of those shapes is pinned below.

No database: these test the AST half only. Whether a column exists is
`test_row_keys_exist`'s job.
"""
from __future__ import annotations

import ast
import textwrap

import sql_row_keys


def _reads(src: str) -> set[tuple[str, str]]:
    """(sql-fragment, key) pairs the analyser links, for one function."""
    tree = ast.parse(textwrap.dedent(src))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return {(" ".join(sql.split()), key)
            for sql, _, key in sql_row_keys.function_reads(fn)}


def test_it_links_a_read_to_the_query_that_produced_it():
    got = _reads("""
        async def f(pool):
            row = await pool.fetchrow("SELECT id, title FROM stories")
            return row["title"]
    """)
    assert got == {("SELECT id, title FROM stories", "title")}


def test_it_follows_a_row_through_a_for_loop():
    got = _reads("""
        async def f(pool):
            rows = await pool.fetch("SELECT id FROM tasks")
            for r in rows:
                print(r["id"])
    """)
    assert got == {("SELECT id FROM tasks", "id")}


def test_each_loop_keeps_its_own_query():
    """The 158-finding bug: one name, two loops, two queries."""
    got = _reads("""
        async def f(pool):
            for row in await pool.fetch("SELECT a FROM one"):
                print(row["a"])
            for row in await pool.fetch("SELECT b FROM two"):
                print(row["b"])
    """)
    assert got == {("SELECT a FROM one", "a"), ("SELECT b FROM two", "b")}


def test_sibling_comprehensions_do_not_share_a_scope():
    """The last false positive: two comprehensions, one target name."""
    got = _reads("""
        async def f(pool):
            files = await pool.fetch("SELECT filename FROM template_files")
            skills = await pool.fetch("SELECT name FROM skills")
            return {
                "f": {r["filename"]: 1 for r in files},
                "s": {r["name"]: 1 for r in skills},
            }
    """)
    assert got == {("SELECT filename FROM template_files", "filename"),
                   ("SELECT name FROM skills", "name")}


def test_a_key_written_onto_the_row_is_not_a_read_of_the_query():
    """`_mark_waiting` stamps `waiting` on each row and reads it back."""
    got = _reads("""
        async def f(pool):
            rows = await pool.fetch("SELECT story_id FROM stories")
            for r in rows:
                r["waiting"] = True
                print(r["waiting"], r["story_id"])
    """)
    assert got == {("SELECT story_id FROM stories", "story_id")}


def test_it_sees_reads_inside_keyword_arguments():
    """`ast.keyword` is not an `ast.expr`.

    Filtering children on `ast.expr` skipped every read passed as a
    keyword argument -- 192 of them across the tree, invisible for
    exactly the reason the bug they would catch is invisible.
    """
    got = _reads("""
        async def f(pool):
            row = await pool.fetchrow("SELECT name FROM goals")
            return make(goal_name=row["name"])
    """)
    assert got == {("SELECT name FROM goals", "name")}


def test_a_rebound_name_stops_being_a_row():
    """Shadowing must drop the binding, not keep judging against it."""
    got = _reads("""
        async def f(pool, other):
            row = await pool.fetchrow("SELECT id FROM stories")
            row = other
            return row["anything"]
    """)
    assert got == set()


def test_a_row_handed_to_another_function_is_not_followed():
    """Under-reporting is the chosen direction; this pins it."""
    got = _reads("""
        async def f(pool):
            row = await pool.fetchrow("SELECT id FROM stories")
            return helper(row)
    """)
    assert got == set()


def test_an_f_string_query_is_not_followed():
    """Its text is unknown until runtime, so there is nothing to check."""
    got = _reads("""
        async def f(pool, cte):
            rows = await pool.fetch(f"{cte} SELECT id FROM priced")
            for r in rows:
                print(r["id"])
    """)
    assert got == set()


def test_fetchval_is_not_a_row():
    got = _reads("""
        async def f(pool):
            v = await pool.fetchval("SELECT count(*) FROM stories")
            return v["nonsense"]
    """)
    assert got == set()
