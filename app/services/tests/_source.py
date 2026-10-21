"""Read a source file with its DOCSTRINGS removed — and only those.

WHY THIS IS A SHARED HELPER AND NOT THREE COPIES. A test that asserts "this
code no longer does X" has to search the code, and every file that stopped
doing X documents that it used to. Searching raw text finds the explanation
and calls it the offence. Three separate tests hit this, and the third one
made it worth extracting.

WHY `ast` AND NOT A REGEX. The obvious stripper is
`re.sub(triple-quote .*? triple-quote, "", src, flags=DOTALL)` and it is
wrong in this codebase: it also eats triple-quoted SQL literals, which are
frequently the exact text being searched for. A test using it reported a
file as clean because the stripper had removed the evidence.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def source(rel: str) -> str:
    """The file, verbatim."""
    return (ROOT / rel).read_text(encoding="utf-8")


def code(rel: str) -> str:
    """The file with docstrings and `#` comments removed.

    Everything else — including triple-quoted SQL — is kept, because that is
    usually what a test is looking for.
    """
    text = source(rel)
    spans = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            spans.append((body[0].lineno, body[0].end_lineno))
    return "\n".join(
        line for n, line in enumerate(text.splitlines(), 1)
        if not any(a <= n <= b for a, b in spans)
        and not line.lstrip().startswith("#"))
