"""Find the SQL written as string literals in the tree.

Used by `test_every_query_prepares`. Kept apart from the test so the
collection rules can be read -- and argued with -- without a database.

WHAT COUNTS AS SQL. A `str` constant whose first word, ignoring leading
whitespace and SQL comments, is SELECT / INSERT / UPDATE / DELETE /
WITH. Python folds adjacent string literals into one constant before we
see them, so a query split over several quoted lines arrives whole.

WHAT IS DELIBERATELY MISSED, and why that is the right trade:

  * **f-strings.** An `ast.JoinedStr` is not a constant and never
    matches. Its text is not known until runtime, so there is nothing to
    prepare. This is also why item 5 of the SQL plan spelled two
    interpolated table names out: an f-string is invisible to this
    sweep as surely as it is to grep.
  * **Fragments.** `story_card_thread.newest_human_thread()` returns a
    parenthesised sub-select that is not a statement. Mark those with a
    `# sql: fragment` comment on, or immediately above, the literal.
  * **Test files.** Their SQL names fixture tables that the real schema
    has never heard of.

The marker is the only opt-out, and it is deliberately ugly so that a
long list of them reads as the problem it would be.
"""
from __future__ import annotations

import ast
import pathlib
import re

# Leading whitespace, then any number of `--` comment lines, then the
# verb. `WITH` first because a CTE is the common shape here.
_STARTS_SQL = re.compile(
    r"\A\s*(?:--[^\n]*\n\s*)*(WITH|SELECT|INSERT|UPDATE|DELETE)\b",
    re.IGNORECASE)

# A LEADING VERB IS NOT ENOUGH. English prose begins with these words
# constantly -- "Delete a lease.", "Update your progress.", "With
# expect=artifact: ..." -- and the first run of this sweep was mostly
# docstrings. A statement must also carry the companion keyword that
# makes it a statement.
_IS_STATEMENT = re.compile(
    r"\bINSERT\s+INTO\b"
    r"|\bDELETE\s+FROM\b"
    r"|\bUPDATE\s+[\w.\"]+\s+SET\b"
    r"|\bSELECT\b[\s\S]*\bFROM\b"
    r"|\bSELECT\s+(?:\d+|pg_\w+|count\s*\(|nextval|currval|set_config)"
    r"|\bWITH\b[\s\S]*\bAS\s*\(",
    re.IGNORECASE)

_MARKER = "# sql: fragment"


def _marked_lines(source: str) -> set[int]:
    """1-based line numbers carrying the opt-out marker."""
    return {i for i, line in enumerate(source.splitlines(), 1)
            if _MARKER in line}


def is_sql(text: str) -> bool:
    """True when this string reads as a standalone SQL statement."""
    return bool(_STARTS_SQL.match(text)) and bool(_IS_STATEMENT.search(text))


def _joinedstr_parts(tree: ast.AST) -> set[int]:
    """`id()` of every Constant that is a PIECE of an f-string.

    An f-string is an `ast.JoinedStr` whose literal runs are ordinary
    `Constant` nodes, so walking the tree finds them even though the
    f-string itself is skipped. Each run is half a statement:
    `agent_registry` prepends a CTE and the literal that remains reads
    `SELECT COUNT(*) FROM instances`, naming a relation that exists
    only in the part spliced in at runtime.

    Those are not checkable and not defects. They are, however, exactly
    what item 5 of the SQL plan was about -- an f-string is invisible
    here, so SQL that matters should not be built as one.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for part in ast.walk(node):
            if isinstance(part, ast.Constant):
                out.add(id(part))
    return out


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """`id()` of every Constant that is a docstring.

    A docstring is prose by definition, and prose beginning "Delete the
    lease AND every sidecar..." is the single largest source of false
    matches. Excluded structurally rather than by guessing at wording.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def from_source(source: str, where: str = "<string>") -> list[tuple[int, str]]:
    """Every SQL literal in `source`, as (line number, sql).

    A literal is skipped when the marker sits on any line it spans, or
    on the line directly above it -- so the comment can go either on the
    opening line or on its own above the assignment.
    """
    tree = ast.parse(source, filename=where)
    marked = _marked_lines(source)
    docstrings = _docstring_nodes(tree)
    interpolated = _joinedstr_parts(tree)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docstrings:
            continue
        if id(node) in interpolated:
            continue
        if not isinstance(node.value, str) or not is_sql(node.value):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        if marked & set(range(start - 1, end + 1)):
            continue
        out.append((start, node.value))
    return out


def _is_test_path(path: pathlib.Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return "tests" in parts or path.name.startswith("test_")


def walk(roots: list[pathlib.Path]) -> list[tuple[pathlib.Path, int, str]]:
    """Every SQL literal under `roots`, excluding test files.

    A file that will not parse is raised, not skipped: a collector that
    quietly covers less than it claims is the failure this whole sweep
    exists to prevent.
    """
    found: list[tuple[pathlib.Path, int, str]] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if _is_test_path(path) or "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for line, sql in from_source(source, str(path)):
                found.append((path, line, sql))
    return found
