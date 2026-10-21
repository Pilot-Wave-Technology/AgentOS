"""No SQL VALUE is ever built by interpolation.

The repo is clean today: 4,634 `$1`-style placeholders and no query
that splices a value into its own text. That is the single property
separating raw SQL from SQL injection, and it is held by habit. This
test holds it by build failure instead -- which matters most once the
repo is public and the next query is written by somebody who has not
read the other 3,342.

WHAT IS FLAGGED: an interpolation standing in a VALUE position --
directly after `=`, `<`, `>`, `(`, `,`, a comparison word like LIKE or
IN, or inside a quoted literal. `WHERE id = {story_id}` is the shape
that lets a crafted id end the statement and begin another.

WHAT IS NOT: an interpolation in a STRUCTURAL position -- a prepended
CTE (`f"{_COST_CTE} SELECT ..."`), a table name, a column list. Those
are not an injection risk when the spliced text is a module constant,
and they are already discouraged for a different reason: an f-string
is invisible to grep and to the PREPARE sweep. Item 5 of the SQL plan
removed the two that mattered.

This runs in the suite rather than as a `check_*` script so it is
carried by the three cloudbuilds that already invoke `run_suite.sh`,
with nothing to wire up in each.
"""
from __future__ import annotations

import ast
import pathlib

# Plain sibling import, NOT `from tests import ...`: `tests/` has no
# __init__.py, and importing through it synthesises a namespace
# package that shadows packages/agentos-ledger-client/tests/ and
# breaks collection of the whole suite.
import sql_literals

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ROOTS = [_ROOT / "app", _ROOT / "kernel"]

# The literal text directly before an interpolation, stripped of
# trailing whitespace, ending in one of these puts it in a value slot.
_VALUE_ENDINGS = ("=", "<", ">", "(", ",", "'", '"', "+", "||")
_VALUE_WORDS = ("like", "in", "values", "between", "ilike", "similar to")


def _is_constantish(node: ast.expr) -> bool:
    """True when the spliced expression cannot carry caller input.

    A MODULE CONSTANT is not an injection. `q.kind = '{PLAN_KIND}'`
    reads as a value splice and is one -- of a value fixed in the
    source, which an attacker cannot reach. The same goes for a tuple
    of states built once at import.

    The rule is deliberately syntactic and strict: a literal, an
    UPPER_CASE name, or an attribute chain rooted in one. A lowercase
    name is a local, a parameter or a loop variable -- which is where
    caller input lives -- and is flagged even when today's value
    happens to be safe, because the next caller decides that.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return node.id.isupper() or node.id.lstrip("_").isupper()
    if isinstance(node, ast.Attribute):
        # `fairness.LAST_ADVANCED_SQL` -- the module is lower case and
        # the constant is not, so judge the attribute, then the root.
        return node.attr.isupper() or _is_constantish(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        # `int(...)` cannot return anything that closes a quote. This
        # is how the interval and LIMIT splices are already written.
        return node.func.id in ("int", "float", "len")
    # Anything else -- a local, a parameter, a call that returns text --
    # is not provably safe HERE. It may well be safe; say so at the
    # site with a marker, where the reason can be read.
    return False


def _ends_in_value_position(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    if stripped.endswith(_VALUE_ENDINGS):
        return True
    tail = stripped.lower().split()
    return bool(tail) and tail[-1] in _VALUE_WORDS


# No leading `#`, so the justification can be written as a Python
# comment (`# sql: safe -- ...`) OR as a SQL one (`-- sql: safe ...`)
# inside a multi-line query, where a Python comment cannot go. Only
# ever put a SQL marker on a line of its own inside a multi-line
# string: `--` comments out the rest of its line, and these queries
# are concatenated without newlines in places.
_SAFE_MARKER = "sql: safe"


def _offenders_in(source: str, where: str) -> list[str]:
    out: list[str] = []
    marked = {i for i, line in enumerate(source.splitlines(), 1)
              if _SAFE_MARKER in line}
    tree = ast.parse(source, filename=where)
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        whole = "".join(p.value for p in node.values
                        if isinstance(p, ast.Constant)
                        and isinstance(p.value, str))
        if not sql_literals.is_sql(whole):
            continue
        before = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                before += part.value
                continue
            spliced = getattr(part, "value", None)
            line = getattr(part, "lineno", node.lineno)
            if (_ends_in_value_position(before)
                    and spliced is not None
                    and not _is_constantish(spliced)
                    # The marker may sit a few lines above: a real
                    # justification runs to two or three lines, and the
                    # first window tried here was one line and rejected
                    # every comment written to explain itself.
                    and not marked & set(range(line - 6, line + 1))):
                snippet = " ".join(before.split())[-70:]
                out.append(f"{where}:{line}  ...{snippet}<HERE>")
            # A non-literal part contributes unknown text; anything
            # after it is judged on its own literal run.
            before = ""
    return out


def test_no_sql_value_is_interpolated() -> None:
    offenders: list[str] = []
    scanned = 0
    for root in _ROOTS:
        for path in sorted(root.rglob("*.py")):
            if sql_literals._is_test_path(path) or "__pycache__" in path.parts:
                continue
            scanned += 1
            offenders += _offenders_in(
                path.read_text(encoding="utf-8"),
                path.relative_to(_ROOT).as_posix())

    # FAIL, NOT PASS, on an empty scan -- the same trapdoor the PREPARE
    # sweep guards. A walk that stops seeing files reports no offenders.
    assert scanned > 400, (
        f"only {scanned} files scanned; the walk has stopped seeing "
        "the tree")
    assert not offenders, (
        "SQL values must be bound as $1 parameters, never interpolated:\n  "
        + "\n  ".join(offenders))
