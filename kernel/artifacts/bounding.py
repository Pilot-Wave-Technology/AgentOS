"""Turn an oversized tool output into a bounded view plus an artifact id.

E7's criterion: a 50 MB log returns 8 KB plus an artifact id, and fetching
the artifact yields the whole log.

HEAD AND TAIL, NOT HEAD. A truncated-at-the-front view of a test run shows
the compiler banner and hides the failure, which is the one thing anybody
wanted. Errors and summaries live at the end. So the bounded view keeps
both ends and says how much it dropped in between.

THE MESSAGE MATTERS AS MUCH AS THE BYTES. A model that sees `[truncated]`
learns only that something is missing. It needs to know the output was
KEPT, how to reach it, and that grepping a 50 MB log is cheaper than
re-running the command that produced it — otherwise the rational move is to
run it again with `| tail`, paying twice for output we already have.
"""

from __future__ import annotations

HEAD_BYTES = 5 * 1024
TAIL_BYTES = 3 * 1024


def bound_output(text: str, artifact_id: str | None, total_bytes: int) -> str:
    """Render the model-facing view of a large output."""
    if artifact_id is None:
        return text

    head = text[:HEAD_BYTES]
    tail = text[-TAIL_BYTES:] if len(text) > HEAD_BYTES + TAIL_BYTES else ""
    dropped = max(0, len(text) - len(head) - len(tail))

    parts = [head]
    if dropped:
        parts.append(
            f"\n\n... [{dropped:,} characters omitted from the middle. "
            f"The full {total_bytes:,}-byte output is stored as artifact "
            f"{artifact_id[:12]} — read any part of it with `artifact_read` "
            f"(offset/length or a pattern). Do NOT re-run the command to see "
            f"the rest; the output is kept.] ...\n\n"
        )
    if tail:
        parts.append(tail)
    return "".join(parts)


def should_store(text: str, threshold: int) -> bool:
    """Only outputs worth an object get one."""
    return len(text.encode("utf-8", errors="replace")) > threshold
