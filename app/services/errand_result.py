"""Ec2f — what a finished run PRODUCED, in words GO and a person can read.

WHAT WAS MISSING. `on_story_done` told GO the story finished and, when the
executor had proposed completion, quoted its claim. For a coding story that
is nearly enough: the work is a branch, and the branch name is in the same
message. For an errand it is nothing at all — the deliverable is an
artifact, the tree is discarded, and GO learned only that something ended.

Two consequences, both observed in the shape of the design rather than in
logs. GO cannot answer "what did that errand find" without reading bytes it
has no reason to hold. And `stories.result_summary` — the field the
assistant writes when a person files a report, and the one every later
reader looks at — stayed empty for every errand ever run.

NULL CLAIM IS NOT AN ERROR. A run can reach DONE without a completion
proposal: a terminal command exited zero, or somebody accepted it by hand.
Printing nothing there reads as "it produced nothing", which is a different
statement. The fallback says how it ended instead.

NEVER CLOBBERS A PERSON'S SUMMARY. `assistant/write.py` sets
`result_summary` when somebody files a report on the story, and a human
sentence beats a generated one every time. This writes only into an empty
field.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Enough to say what each artifact is; not so much that a GO wake becomes
# a document. The manifest is there for anyone who wants the rest.
SUMMARY_CHARS = 160
MAX_LISTED = 8


_HEADS = """
    SELECT artifact_id, role, name, summary, bytes, supersedes, created_at
      FROM kernel.artifact_manifest
     WHERE {where}
     ORDER BY id DESC
"""


def _heads(rows) -> list[dict]:
    """Drop superseded rows. A chain is a history and its head is the answer:
    listing every version of one report as if they were separate deliverables
    is how a reader ends up with three answers and no current one."""
    superseded = {r["supersedes"] for r in rows if r["supersedes"]}
    return [dict(r) for r in rows if r["artifact_id"] not in superseded]


async def artifacts_for(pool, execution_id: int) -> list[dict]:
    """What this execution emitted, newest first, superseded ones dropped."""
    return _heads(await pool.fetch(
        _HEADS.format(where="execution_id = $1"), execution_id))


async def artifacts_for_story(pool, story_id: str) -> list[dict]:
    """The same, for a LOCAL run -- which has no execution to key on.

    A local errand's whole deliverable is its artifacts, so without this the
    GO report for one says it finished and names nothing it produced: exactly
    the gap `artifacts_for` was written to close, reopened by the other lane.
    """
    return _heads(await pool.fetch(
        _HEADS.format(where="story_id = $1"), story_id))


def render(artifacts: list[dict]) -> str:
    """One line per artifact: role, name, size, and what it says."""
    if not artifacts:
        return ""
    lines = []
    for a in artifacts[:MAX_LISTED]:
        summary = (a.get("summary") or "").strip()
        size = f"{(a.get('bytes') or 0) / 1024:.1f} KB"
        lines.append(f"  · {a['role']}: {a['name']} ({size})"
                     + (f" — {summary[:SUMMARY_CHARS]}" if summary else ""))
    if len(artifacts) > MAX_LISTED:
        lines.append(f"  · … and {len(artifacts) - MAX_LISTED} more")
    return "\n".join(lines)


async def summarise(pool, execution_id: int, claim: str | None = None) -> str:
    """The finished run in a few lines, for GO and for `result_summary`."""
    artifacts = await artifacts_for(pool, execution_id)
    parts = []
    if artifacts:
        parts.append(f"Produced {len(artifacts)} artifact(s):")
        parts.append(render(artifacts))
    if claim:
        parts.append(f"claimed: {claim[:400]}")
    if not parts:
        # THE FALLBACK. A run that proposed nothing and emitted nothing
        # still ended somehow, and how it ended is the only honest thing
        # to say. `terminal_command` is the errand path that predates
        # propose_completion; a hand-accepted run has neither.
        row = await pool.fetchrow(
            "SELECT state, terminal_command, stall_reason "
            "FROM kernel.executions WHERE id = $1", execution_id)
        if row is None:
            return ""
        how = (f"its terminal command `{row['terminal_command']}` succeeded"
               if row["terminal_command"]
               else "it was accepted without a completion claim")
        parts.append(f"Reached {row['state']} with no artifacts recorded — "
                     f"{how}.")
    return "\n".join(parts)


async def record(pool, story_id: str, execution_id: int,
                 claim: str | None = None) -> str:
    """Write the summary onto the story, unless somebody wrote a better one.

    Returns what was written, or "" when there was nothing to say or a
    human summary was already there.
    """
    if not story_id:
        return ""
    text = await summarise(pool, execution_id, claim)
    if not text:
        return ""
    # `WHERE result_summary IS NULL OR = ''` in the statement rather than
    # a read-then-write: the assistant's report path writes the same
    # column, and a check followed by an update is a race with it.
    written = await pool.fetchval(
        """
        UPDATE stories SET result_summary = $2, updated_at = now()
         WHERE id = $1 AND COALESCE(btrim(result_summary), '') = ''
        RETURNING id
        """, story_id, text)
    if written:
        logger.info("errand_result: wrote result_summary for story %s "
                    "from execution %s", story_id, execution_id)
        return text
    return ""
