"""Reading part of a stored artifact — a byte range, or the lines that match.

The point of the store is that a big output stays REACHABLE. A read that
could only return the whole object would put the 50 MB back into the
context the bounding just kept out of it, so a read is a range or a grep
and never the thing itself.

WHY THIS IS ITS OWN MODULE. Two callers want exactly these bytes and
exactly these limits, and they have different shapes: the executor's
`artifact_read`, which wraps the result in a `ToolResult` and reports
failure as an exit code, and the assistant's `read_artifact`, which has
no ToolResult, no sandbox and no exit code. Only the envelope differs.
Leaving the logic inside the executor's tool would have meant the
assistant either importing `ToolResult` — coupling a surface that has no
sandbox to one that does — or growing a second copy of the grep cap and
the range arithmetic, which then drift.

So the envelope stays at the call site and the bytes live here.
"""

from __future__ import annotations

import re

from kernel.artifacts.store import get_store

# Matching lines are capped: a pattern that hits every line of a 50 MB
# log is the same problem the store exists to solve, arriving by a
# different route. The count of what was dropped is reported, because a
# silently truncated grep reads as "that is all there is".
MAX_MATCHES = 200

# A range with no length asked for. Deliberately the same 8 KB the
# bounded view uses, so "read more of it" costs what the first look did.
DEFAULT_LENGTH = 8192


class BadPattern(Exception):
    """The caller's regex does not compile. Carries what re said."""


def read_pattern(artifact_id: str, pattern: str, total: int) -> str:
    """The lines matching `pattern`, numbered, capped at MAX_MATCHES."""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise BadPattern(str(exc)) from None

    body = get_store().get(artifact_id).decode("utf-8", errors="replace")
    hits = [f"{n}:{line}" for n, line in enumerate(body.splitlines(), 1)
            if rx.search(line)]
    if not hits:
        return f"(no lines match {pattern!r} in {total:,} bytes)"

    shown = hits[:MAX_MATCHES]
    out = "\n".join(shown)
    if len(hits) > len(shown):
        out += f"\n... [{len(hits) - len(shown):,} more matching lines]"
    return out


def read_range(artifact_id: str, offset: int, length: int, total: int) -> str:
    """A byte range, with a header saying which part of what it is.

    A negative offset reads from the end — `offset=-2000` is the last two
    thousand bytes, which is where a test runner puts the failure.
    """
    length = length or DEFAULT_LENGTH
    offset = offset or 0
    if offset < 0:
        offset = max(0, total + offset)

    chunk = get_store().read_range(artifact_id, offset, length)
    text = chunk.decode("utf-8", errors="replace")
    header = (f"[artifact {artifact_id[:12]}, bytes {offset:,}-"
              f"{offset + len(chunk):,} of {total:,}]\n")
    return header + text
