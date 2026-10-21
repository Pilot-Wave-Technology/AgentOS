"""What a claim is measured against before it is recorded.

ARTIFACTS, MECHANICALLY. Not the command checks, and that division is
the point rather than an omission.

An artifact check is a question about the RECORD: did this run emit
something with that role, does it parse, does it have bytes in it. The
answer is one indexed read of `artifact_manifest`, it costs nothing, and
the executor could not fake it without actually emitting the thing. So
it is measured here, at the claim, where a failure comes straight back
as the tool result and the run carries on with the context still live --
rather than parking a person to tell them the deliverable is missing.

A COMMAND CHECK IS NOT RUN HERE, and used to be. It ran as a foreground
`bash` call pinned to `BASH_MAX_S`, which refuses anything over two
minutes -- so a repository whose suite takes longer could never claim at
all: exit 124, non-zero, `fail`, bounce, repeat. Lifting that meant
running a possibly hour-long suite at every claim attempt, in the same
sandbox, over a tree the model had just finished testing. That is a
second full run of work the executor already did, to produce an answer
it already has.

So the executor RUNS its tests as work and REPORTS what they said --
passed, failed, skipped, errors, against a baseline it took before
touching anything -- and a person reads those numbers. When the platform
switch turns verification on, the command checks are re-run mechanically
in a clean container, which is what that switch is FOR. Off, they are
the executor's word; on, they are measured. The person is the last word
either way.

WHAT THIS COSTS, said plainly because it is the trade a platform admin
is making when they leave verification off: a story can report "41
passed, 0 failed" over a red suite and reach a person. The baseline and
the four counts make that visible -- a claim with no numbers in it, or
numbers that do not move, is the thing to look at -- but nothing here
prevents it. An artifact that was never emitted still cannot get past
this point, which is why errands are unaffected.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Enough of a failure to act on, short enough not to drown the turn.
TAIL = 1200


async def checks_for(conn, execution_id: int) -> list:
    """The criteria this run's contract says define done.

    Read from `criteria`, which is what the VERIFIER would run -- not
    from the plan's payload, which is what was proposed. Where a human
    edited a check at approval, the criteria carry the edit and the
    payload does not.
    """
    rows = await conn.fetch(
        """
        SELECT c.id, c.spec::text AS spec, c.label, c.kind
          FROM criteria c
          JOIN executions e ON e.contract_id = c.contract_id
         WHERE e.id = $1 AND NOT c.held_out
         ORDER BY c.id
        """, execution_id)
    out = []
    for r in rows:
        try:
            spec = json.loads(r["spec"] or "{}")
        except (ValueError, TypeError):
            continue
        run = (spec.get("run") or "").strip()
        if run:
            # `kind` TRAVELS WITH THE CHECK. Dropping it here sent an
            # artifact check to bash -- see `run_own_checks`.
            out.append({"run": run, "label": r["label"],
                        "kind": r["kind"] or "command",
                        "id": r["id"], "spec": spec})
    return out


async def run_own_checks(conn, execution_id: int) -> dict:
    """Measure what can be measured here. Returns a report.

    `{"ran": n, "failed": [...], "unmeasured": n, "skipped": reason}`.
    A failure carries the check and what the record said, because
    "a check failed" is not actionable and "this run emitted
    log:run.log" is.
    """
    checks = await checks_for(conn, execution_id)
    artifacts = [c for c in checks if c["kind"] == "artifact"]
    if not artifacts:
        # Nothing mechanical to ask. Not an error: a story whose checks
        # are all commands has nothing for this to measure, and refusing
        # the claim over that would strand every story.
        return {"ran": 0, "failed": [], "skipped": "", "unmeasured":
                len(checks)}

    # Ec2d — AN ARTIFACT CHECK IS A QUESTION ABOUT THE MANIFEST, not a
    # shell line. `kernel/verifier/dryrun.py` split on `kind` for exactly
    # this reason and said why: shelling out `artifact[role=report]`
    # produces "command not found", "an answer about the image, dressed
    # as an answer about the check". That reasoning was written in the
    # dry-run and never reached here -- so at `propose_completion` every
    # artifact check was bashed and failed. The executor, told its goal
    # check could not run, asked to re-register the plan; the plan came
    # back identical because it was already correct; and the run parked
    # for approval again. Seven generations on KER25 before anyone
    # looked at what `checks_for` actually selected.
    from kernel.verifier.artifact_kind import PASS, evaluate

    failed = []
    for c in artifacts:
        outcome = await evaluate(conn, execution_id, c)
        if outcome.passes != PASS:
            failed.append({
                "run": c["run"], "label": c["label"],
                "output": str((outcome.detail or {}).get("reason") or "")})
    return {"ran": len(artifacts), "failed": failed, "skipped": "",
            "unmeasured": len(checks) - len(artifacts)}


def refusal_text(report: dict) -> str:
    """What the executor is told when the record does not back its claim."""
    lines = [
        f"{len(report['failed'])} of {report['ran']} artifact checks "
        f"FAILED, so this is not done yet. These are measured from the "
        f"harness's own record of what you emitted, not from what you "
        f"say about it -- so the fix is to emit the thing, not to "
        f"describe it differently.",
        "",
    ]
    for f in report["failed"]:
        tag = f"[{f['label']}] " if f.get("label") else ""
        lines += [f"{tag}{f['run']}", "", (f["output"] or "(no output)"), ""]
    lines.append("Emit what is missing and propose completion again. If a "
                 "check is WRONG rather than unmet, say so with "
                 "ask_human -- do not edit the check to pass.")
    return "\n".join(lines)
