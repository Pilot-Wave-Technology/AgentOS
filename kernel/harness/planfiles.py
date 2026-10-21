"""The plan, written into every repo it is about to change.

A plan that lives only in a database row is invisible at the one place
the work is reviewed: the repository. Written into each writable git
mount as

    agentos_plans/<story_id>/plan-NN.md

it rides the story's own commits, shows up in the PR diff for the human
who merges, and lands on trunk as the durable record of what was
promised — the shape the old operator setup used, carried forward.

CYCLES AND REVISIONS. NN is the story's cycle: one more than the number
of claims already ANSWERED. Revising a plan before the work is done
overwrites the same file — the cycle has not changed, and ten files for
ten rewordings of one intent is noise. A story sent back for more work
gets plan-02.md: a new cycle is new intent, and the old plan is history
worth keeping.

ANSWERED, NOT VERIFIED, and that distinction is the whole of it. This
counted passed verdicts, which only exist when the Verifier runs — so
with verification off every cycle was cycle 1 and each new plan
overwrote the last one. An errand, never verified at all, could never
produce anything but plan-01.md.

NEVER FATAL. The plan is registered in the database first; the file is
the mirror. A workspace that cannot be written (reaped sandbox, odd
permissions) logs and moves on rather than blocking an approval on a
courtesy copy.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import functools
import logging
import os

logger = logging.getLogger(__name__)

NL = chr(10)
FOLDER = "agentos_plans"


def _render(story_id: str, cycle: int, payload: dict,
            registered_at=None) -> str:
    steps = payload.get("steps") or []
    checks = payload.get("checks") or []
    lines = [
        f"# Plan — story {story_id} (cycle {cycle})",
        "",
        f"_Registered {(registered_at or _dt.datetime.now(_dt.timezone.utc)):%Y-%m-%d %H:%M} UTC._",
        "",
        payload.get("rationale") or "",
        "",
        "## Steps",
        "",
    ]
    lines += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    lines += ["", "## Verified by", ""]
    for c in checks:
        lines.append(f"- **[{c.get('label', '?')}]** `{c.get('run', '')}`")
    lines += ["", "_Revisions in this cycle overwrite this file; a new "
                  "cycle after a done phase gets the next number._", ""]
    return NL.join(lines)


async def persist_plan(conn, execution_id: int, payload: dict,
                       registered_at=None) -> list[str]:
    """Write the plan into every writable git mount. Returns the paths.

    `registered_at` pins the timestamp in the file, so writing the same
    plan again (`mirror_canned_plan`, once per leg) produces the same
    bytes and no change for git to see."""
    from kernel.workspace.mounts import mounts_for

    row = await conn.fetchrow(
        "SELECT workspace_path, story_id FROM executions WHERE id = $1",
        execution_id)
    if not row or not row["workspace_path"]:
        return []
    story = row["story_id"] or str(execution_id)
    # ── WHAT COUNTS AS A FINISHED CYCLE ──────────────────────────────
    #
    # This counted PASSED VERDICTS, and a verdict is written by the
    # Verifier. So the number only moved on a run that was verified:
    # with verification switched off -- the default -- a story's second
    # plan was still cycle 1 and OVERWROTE plan-01.md. Run 283 did
    # exactly that: approve, acceptance answered `revise`, a second plan
    # approved at 10:36, zero verdicts, both plans written to the same
    # file. The re-plan worked; the history of it was destroyed.
    #
    # An errand never had a hope of this working -- it is never verified
    # at all -- but the defect was never errand-specific, it just showed
    # there first and always.
    #
    # THE SAME DEFINITION THE PLAN GATE USES: a cycle ends when its
    # claim is ANSWERED. Every finished cycle parks on an acceptance and
    # exactly one -- `worker/loop.py` on the unverified path,
    # `verifier/finish_pass.py` on the verified one -- so answered
    # acceptances count cycles on both substrates without
    # double-counting a verified run, which produces a verdict AND an
    # acceptance for the same cycle.
    #
    # MAX, NOT SUM, for that reason, and the verdict arm is kept because
    # `finish_pass` can finish without parking when nobody must accept;
    # that cycle has a verdict and no acceptance, and reopening it would
    # otherwise reuse its number.
    #
    # AND A LANDING IS A CLAIM TOO. A story holding its workspace for
    # the merge parks on `landing`, not `acceptance`; see `cycles.py`
    # for BMIF1, where four later plans each overwrote plan-01.md.
    from kernel.harness.cycles import finished_cycles
    cycle = 1 + max(
        await finished_cycles(conn, execution_id),
        await conn.fetchval(
            "SELECT count(*) FROM verdicts WHERE execution_id = $1 AND passed",
            execution_id) or 0)
    body = _render(story, cycle, payload, registered_at)

    mounts = await mounts_for(conn, execution_id)

    # WHERE THE TREE IS, WHICH IS NOT WHERE THIS CODE RUNS.
    #
    # The leg executes in the conductor. On Docker the conductor shares
    # the workspace volume, so writing `/workspace/...` here landed in
    # the story's checkout and the plan rode its commits into the PR --
    # which is the whole point of mirroring it. On Kubernetes the tree
    # is in the execution's pod and the conductor has no such path, so
    # every mirror failed with
    #
    #   plan file for 191 not written under repos/...:
    #   [Errno 13] Permission denied: '/workspace'
    #
    # and the structure quietly stopped existing on the substrate that
    # matters. Same shape as `story_commit`, and the same answer: the
    # work crosses to the pod and runs THERE, through the same function,
    # so there are not two implementations of what a plan file is.
    from kernel.workspace.browse import workspace_host
    from kernel.workspace.podfs_client import (WorkspaceUnreachable,
                                               plan as remote_plan,
                                               tree_is_remote)

    # `tree_is_remote` AND NOT `is_pod(resolve())`, and the difference is
    # the whole bug. This runs in the LEG, which after the cutover is
    # inside the execution pod -- where `is_pod` is true, the tree is
    # nevertheless mounted locally, and the crossing below asks the
    # Kubernetes API a question `agentos-execution` has no Role to ask.
    # It fails as `WorkspaceUnreachable`, which the handler swallows at
    # WARNING, so the plan file is simply never written and nothing
    # anywhere says a story lost its plan.
    if not tree_is_remote():
        return write_files(row["workspace_path"], mounts, story, cycle, body)

    try:
        host = await workspace_host(conn, execution_id)
        return await asyncio.to_thread(
            functools.partial(remote_plan, host, row["workspace_path"],
                              mounts, story, cycle, body,
                              execution_id=execution_id))
    except WorkspaceUnreachable as e:
        # A courtesy copy must not block an approval -- the caller says
        # so and catches anything -- but it must say which half failed.
        logger.warning("plan file for %s not mirrored: %s", execution_id, e)
        return []


def write_files(root: str, mounts, story: str, cycle: int,
                body: str) -> list:
    """Write the plan into every writable git mount, wherever this runs.

    The one implementation. Called directly where the tree is local and
    through `podfs.op_plan` where it is in a pod.
    """
    written = []
    for m in mounts:
        if not (m.writable and m.binding == "git"):
            continue
        base = os.path.join(root, *m.at.split("/")) if m.at else root
        target = os.path.join(base, FOLDER, story)
        try:
            os.makedirs(target, exist_ok=True)
            path = os.path.join(target, f"plan-{cycle:02d}.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            written.append(f"{m.at or '.'}/{FOLDER}/{story}/plan-{cycle:02d}.md")
        except OSError as e:
            logger.warning("plan file not written under %s: %s",
                           m.at or ".", e)
    return written


async def mirror_canned_plan(conn, execution_id: int) -> list[str]:
    """Write the live plan's file when the PLATFORM wrote the plan.

    A plan the executor registers is mirrored by `register_plan`. The
    trunk-integration plan is written by the control plane and approved
    on a card, so nothing in that path ever touched the tree: with one
    approval covering the whole catch-up there was no plan-02.md at all,
    only plan-01.md. This runs where the leg reads the live plan's scope
    -- inside the pod, where the tree is -- and pins the timestamp to the
    approval, so a later leg rewrites identical bytes.
    """
    row = await conn.fetchrow(
        """
        SELECT payload, answered_at FROM questions
         WHERE execution_id = $1 AND kind = 'authorization'
           AND status = 'ANSWERED' AND NOT COALESCE(declined, false)
         ORDER BY id DESC LIMIT 1
        """, execution_id)
    if row is None:
        return []
    payload = row["payload"]
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    if not (isinstance(payload, dict) and payload.get("platform_canned")):
        return []
    try:
        return await persist_plan(conn, execution_id, payload,
                                  registered_at=row["answered_at"])
    except Exception as exc:                                # noqa: BLE001
        logger.warning("canned plan for %s not mirrored: %s",
                       execution_id, exc)
        return []
