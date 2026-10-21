"""Evidence that a command already ran, against the tree it ran against.

THE DEFECT THIS EXISTS FOR, in the words of the brief: a long suite runs
once while the executor is working and again when the same suite is
named as a check, in the same cycle, against a tree nothing has touched
in between. Forty minutes, twice, for one measurement.

THE KEY IS (command, tree), AND IT HAS TO BE. Keying on the criterion
makes the two runs different events -- the first was a bare command, the
second is criterion 3 -- which is exactly how the duplication survived
being noticed. Keying on HEAD makes them the same event when they are
not: an executor that edits and does not commit leaves HEAD identical
over a different tree, and reusing a result there is a green tick over
code nothing has run. So: the working tree, from `treefp`.

TAKEN TWICE, NOT PER POLL. Once when the job starts and once when it
ends, and evidence counts only when the two agree -- a suite that passed
while its inputs were being edited underneath it measured nothing in
particular. A fingerprint per poll would also be a `git add -A` per
poll, which on a two-hour job is hundreds of them.

WHAT IS NEVER DONE HERE. Nothing is reused silently. `run_check` says,
in the result the model reads, that the answer came from a recorded run
and names the job it came from -- because a check that reports a pass
nobody can locate is worse than one that takes forty minutes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def take(ex) -> str:
    """The workspace's fingerprint now, or "" if it cannot be had.

    Empty is read everywhere as "no evidence is usable", never as
    "nothing changed": the safe direction is always to run the check.
    """
    from kernel.bridge.client import call_bridge

    extras: dict = {}
    try:
        result = await call_bridge(
            "tree_fingerprint", {}, "claim", port=ex._port, host=ex._host,
            timeout=180, extras=extras)
    except Exception:                                       # noqa: BLE001
        logger.debug("fingerprint unavailable", exc_info=True)
        return ""
    if result.error:
        return ""
    return str(extras.get("fingerprint") or "")


async def stamp(ex, facts: dict) -> dict:
    """Add the start and end fingerprints to a job's facts, once each.

    Called from the mirror path, which runs on every poll; the state on
    `ex` is what keeps this twice-per-job rather than per-report.
    """
    job_id = str(facts.get("id") or "")
    if not job_id:
        return facts
    seen = getattr(ex, "_claim_fp", None)
    if seen is None:
        seen = {}
        ex._claim_fp = seen
    entry = seen.get(job_id)
    if entry is None:
        entry = {"start": await take(ex), "end": ""}
        seen[job_id] = entry
        out = dict(facts)
        out["fingerprint"] = entry["start"]
        return out
    if not facts.get("running") and not entry["end"]:
        entry["end"] = await take(ex)
        out = dict(facts)
        # BOTH, on the closing report. The row may never have received
        # the opening one -- the panel was down, the throttle had not
        # elapsed -- and evidence with only half a key is unusable.
        out["fingerprint"] = entry["start"]
        out["fingerprint_end"] = entry["end"]
        return out
    return facts


async def prior_run(conn, execution_id: int, command: str, fingerprint: str):
    """A finished job in this story that ran `command` over this tree.

    Both fingerprints must match: the tree was the same when the job
    started, when it ended, and now. The newest such run wins, and a
    story with no id -- an errand, a bare execution -- finds nothing,
    which is correct rather than a special case.
    """
    if not command.strip() or not fingerprint:
        return None
    return await conn.fetchrow(
        """
        SELECT j.job_id, j.exit_code, j.ended, j.produced, j.updated_at
          FROM kernel.execution_jobs j
          JOIN kernel.executions e ON e.id = j.execution_id
         WHERE e.story_id = (SELECT story_id FROM kernel.executions
                              WHERE id = $1)
           AND e.story_id IS NOT NULL
           AND j.command = $2
           AND j.fingerprint = $3
           AND j.fingerprint_end = $3
           AND j.running = false
           AND j.exit_code IS NOT NULL
         ORDER BY j.updated_at DESC
         LIMIT 1
        """,
        execution_id, command, fingerprint)
