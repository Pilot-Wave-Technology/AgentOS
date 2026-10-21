"""Push a finished execution's work to where the team can get it.

THE HOLE THIS CLOSES. `story_commit` commits; `GitDriver.publish`
pushes; and NOTHING connected them to DONE. The first live story ended
with seven commits sitting in a sandbox volume — [ahead 7] — while the
git engine merged the remote branch it had cut, which contained
nothing, marked the node MERGED, and told the GO the story had landed.
Every part of that chain behaved correctly against a lie: "done" never
meant "delivered".

Called by the adjudicator BEFORE it grants DONE, and a push that fails
refuses the transition — loudly, with the story returned to RUNNING so
the loop retries and a person can see why. A "done" the team cannot
pull is not done; it is the [ahead 7] again with better manners.

WHY AFTER THE VERDICT AND NOT ANYWHERE EARLIER. Three places were
nearer to hand and each costs something this one does not:

  * inside `story_commit`. Every leg would push, so the engine's branch
    becomes a moving target for a merge queue already watching it, and a
    network blip turns a checkpoint into a failed tool call — the story
    loses the commit because the push failed, which is backwards.

  * at `propose_completion`, or on entering VERIFYING. A rejected
    completion returns to RUNNING (`completion_rejected` is a real
    edge), so the branch would carry attempts the verifier threw out and
    the merge queue could land one.

  * a worker-side tool the model calls. Delivery would then be the
    model's decision, and the run that produced the [ahead 7] proves how
    that ends: an agent that simply never calls it leaves the work
    behind and nothing downstream can tell the difference.

Worker-side is not a choice here but a constraint: git credentials are
worker-private by construction (`kernel/workspace/git_bearer.py` — the
sandbox shares the network namespace, not the mount namespace), and the
adjudicator runs alongside them.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os

from kernel.workspace.composite import MOUNT_MISSING, CompositeDriver
from kernel.workspace.driver import CheckpointRef
from kernel.workspace.mounts import mounts_for

logger = logging.getLogger(__name__)


async def _pod_host(conn, execution_id: int) -> str:
    """The pod holding this tree, or "" when it is on this filesystem.

    WHY THIS EXISTS AT ALL. Everything below runs `git` against
    `workspace_path`. On Docker and E0 that path is this process's own
    filesystem and always was. On a pod substrate it NEVER is: the
    workspace is a ReadWriteOncePod claim mounted in the execution pod's
    worker container, and the conductor has no such directory.

    So every writable mount read as MOUNT_MISSING, `publish_execution`
    correctly refused to claim delivery, and the executor was told "your
    work PASSED verification but could not be pushed" -- forever. No
    story on this substrate could reach DONE. Measured on execution 203,
    whose branch sat [ahead 1] with a real commit in it.

    `story_commit` had already been routed through podfs for exactly
    this reason (kernel/harness/executor_worker_dispatch.py::worker_execute).
    Publish was the half that never followed.

    THE CITATION MOVED, AND THE CODE DID NOT. Wave 4a split
    `harness/executor.py` 359 -> 200 (plan 11.5f) and `_worker_execute`
    went with the dispatch table, losing its underscore on the way out.
    Repointed here rather than generalised to "somewhere in the
    harness": a reference a reader cannot follow is how 11.5f's
    path-pinned guards came to prove nothing at all.
    """
    from kernel.drivers.registry import resolve             # noqa: PLC0415
    from kernel.workspace.podfs_client import is_pod        # noqa: PLC0415

    if not is_pod(resolve()):
        return ""
    from kernel.workspace.browse import workspace_host      # noqa: PLC0415

    return await workspace_host(conn, execution_id)


async def publish_execution(conn, execution_id: int) -> tuple[bool, str]:
    """(ok, detail). ok=True when every writable mount is delivered."""
    root = await conn.fetchval(
        "SELECT workspace_path FROM executions WHERE id = $1", execution_id)
    if not root:
        # Nothing was mounted, so there is nothing undelivered. The
        # errand profile and the corpus stories live here.
        return True, "no workspace"
    mounts = await mounts_for(conn, execution_id)
    writable = [m for m in mounts if m.writable]
    if not writable:
        return True, "no writable mounts"

    # ── AN ERRAND DELIVERS ARTIFACTS, NEVER COMMITS ─────────────────
    #
    # D1 below already held an errand's TRUNK-LANE mounts back, on the
    # rule that "what an errand must never do is LAND that work". This
    # is the same rule over the whole workspace, and it is here rather
    # than inside D1 so an errand never pays for the lane guard's
    # round-trip to the pod either.
    #
    # WHAT CHANGED AND WHY. Every errand repo is now granted writable
    # (`kernel/go/errand_resources.py` upgrades it), because with all
    # mounts read-only `writable_scope_for` returns [] and the `write`
    # tool refuses everywhere -- leaving the model to write its report
    # through a shell heredoc and trip the destructive-command gate on
    # every attempt. A writable checkout is SCRATCH, so the push has to
    # stop or an errand's working files reach the forge.
    #
    # The earlier comment argued the opposite -- that holding the push
    # back "would strand the errand's own branch, which is exactly the
    # work we do want kept". That was true when the only way an errand
    # could keep anything was a commit. It is not how an errand keeps
    # work: `profiles/errand-ops/prompt.md` is explicit that its branch
    # is deleted on acceptance and `emit_artifact` is the road out.
    # Pushing bought a branch that gets thrown away.
    #
    # A STORY IS UNAFFECTED. This returns before anything a story
    # reaches, and the D1 refusal below still fires for one.
    kind = await conn.fetchval(
        "SELECT kind FROM executions WHERE id = $1", execution_id)
    if kind == "errand":
        logger.info(
            "execution %s is an errand: holding back all %d writable "
            "mount(s) -- an errand delivers artifacts, not commits",
            execution_id, len(writable))
        return True, ("nothing pushed: an errand's workspace is scratch "
                      "and its deliverable is an artifact")

    # WHERE the git commands have to run. Resolved once and threaded
    # through both the lane check and the push, because a lane check
    # that reads a different filesystem from the push is a guard that
    # cannot fail and therefore is not a guard.
    from kernel.workspace.podfs_client import (               # noqa: PLC0415
        WorkspaceOpFailed, WorkspaceUnreachable, heads as remote_heads,
        publish as remote_publish)
    try:
        host = await _pod_host(conn, execution_id)
    except Exception as exc:                                  # noqa: BLE001
        return False, f"could not locate the workspace pod: {exc}"
    # A STORY RUN NEVER PUSHES A LANE. The workspace should be on the
    # engine's feature branch; a mount sitting on a trunk-lane branch
    # means the ref derivation went wrong somewhere upstream, and
    # pushing it would deliver unreviewed work straight to the merge
    # target -- observed once: a frozen pre-fix mount checked out
    # cal_frontend itself and the whole story landed with no PR.
    lanes = {r["branch_name"] for r in await conn.fetch(
        """
        SELECT gr.branch_name FROM goal_resources gr
          JOIN executions e ON e.goal_id = gr.goal_id
         WHERE e.id = $1 AND gr.kind = 'branch' AND gr.role = 'trunk'
           AND gr.branch_name IS NOT NULL
        """, execution_id)}
    if lanes:
        import subprocess

        def _heads() -> dict:
            out = {}
            for m in writable:
                if m.binding != "git":
                    continue
                p = os.path.join(root, *m.at.split("/")) if m.at else root
                try:
                    out[m.at] = subprocess.run(
                        ["git", "-C", p, "rev-parse", "--abbrev-ref",
                         "HEAD"], capture_output=True, text=True,
                        timeout=30).stdout.strip()
                except Exception:                           # noqa: BLE001
                    out[m.at] = ""
            return out

        if host:
            try:
                got = await asyncio.to_thread(
                    functools.partial(remote_heads, host, root, writable,
                                      execution_id=execution_id))
            except (WorkspaceOpFailed, WorkspaceUnreachable) as exc:
                # BOTH, because either way the guard did not run -- and a
                # lane guard that could not read the tree must never be
                # treated as having passed. That silent pass is what let
                # a story sit on its own merge target undetected.
                return False, (
                    f"could not read the branch of each mount from the "
                    f"workspace pod, so the trunk-lane guard could not "
                    f"run and nothing was pushed: {exc}")
            heads = got.get("heads") or {}
        else:
            heads = await asyncio.to_thread(_heads)
        on_lane = {at: b for at, b in heads.items() if b in lanes}
        if on_lane:
            # D1 — A STORY NEVER PUSHES A LANE, and this is the second
            # of its two enforcement points: a commit in a trunk
            # checkout reaches trunk by push alone, with no merge
            # involved and no node for the merge chokepoint to skip.
            #
            # A mount on a lane means the ref derivation went wrong
            # upstream, and pushing would deliver unreviewed work
            # straight to the merge target. Refuse, loudly.
            #
            # THIS USED TO BRANCH ON KIND. An errand was allowed here
            # and had only its lane mounts held back, because an errand
            # may legitimately be granted trunk writable -- some checks
            # are written against trunk and running them changes files.
            # An errand no longer reaches this point at all: it returns
            # above, with EVERY writable mount held back, so the kind
            # test here had exactly one answer left.
            return False, (
                "refusing to push: workspace mount(s) are checked out "
                "on a trunk lane "
                + ", ".join(f"{at}@{b}" for at, b in on_lane.items())
                + " -- a story pushes its feature branch, never the "
                  "merge target. The mount ref derivation needs fixing "
                  "before this work can be delivered.")

    try:
        if host:
            # IN THE POD, where the tree and the git credentials both
            # are. The same CompositeDriver, the same mounts, the same
            # CheckpointRefs back -- only the container differs.
            got = await asyncio.to_thread(
                functools.partial(remote_publish, host, root, writable,
                                  execution_id=execution_id))
            # A REPLY WITHOUT `refs` IS NOT AN EMPTY PUSH. `.get(...) or
            # []` made those two the same thing and cost execution 203 a
            # false DONE: no refs means no absent mounts, which means
            # (True, "nothing to push"). Asked explicitly so a malformed
            # or unexpected reply refuses instead of finishing the story.
            if "refs" not in got:
                return False, (
                    "the workspace pod answered the push without saying "
                    "what it pushed, so nothing can be claimed as "
                    f"delivered. It said: {str(got)[:300]}")
            refs = tuple(CheckpointRef(kind=r.get("kind") or "none",
                                       ref=r.get("ref") or "",
                                       at=r.get("at") or "",
                                       detail=r.get("detail") or "")
                         for r in got["refs"])
        else:
            # Sync subprocess work (git push per mount) off the loop.
            refs = await asyncio.to_thread(
                CompositeDriver().publish, root, tuple(writable))
    except WorkspaceOpFailed as exc:
        # THE POD ANSWERED. Whatever git said is the whole diagnosis and
        # it travels verbatim -- a 403 from the forge is a permission to
        # grant, not a pod to go looking for.
        logger.warning("publish for %s failed in the pod: %s",
                       execution_id, exc)
        return False, f"the push was attempted and failed: {exc}"
    except WorkspaceUnreachable as exc:
        # NOT a delivery failure the executor can fix by editing code.
        # Said separately so the message it is handed names the pod
        # rather than sending it to look at the branch.
        logger.warning("publish for %s could not reach the workspace pod: "
                       "%s", execution_id, exc)
        return False, (f"the workspace pod could not be reached, so "
                       f"nothing was pushed: {exc}")
    except Exception as e:                                  # noqa: BLE001
        logger.warning("publish for %s failed: %s", execution_id, e)
        return False, f"{type(e).__name__}: {e}"

    # ── AN ABSENT MOUNT IS NOT AN EMPTY ONE ──────────────────────────
    #
    # "no remote" (a corpus origin, E0) and "copy-back needs Stage H
    # credentials" are honest successes: the mount was looked at and had
    # nowhere to go. A mount that was not on disk is the opposite — it
    # was never looked at, so nothing can be said about its commits, and
    # returning True here is precisely the false "delivered" this module
    # exists to prevent. The reachable cause is a workspace the
    # adjudicator cannot see: `replicas: 2` is deliberate, the tree is
    # local to one conductor, and the tick that adjudicates need not be
    # the one that cloned. Refusing puts it on `stall_reason` and in
    # front of the executor instead of on the board as done.
    absent = [r.at or "." for r in refs if r.detail == MOUNT_MISSING]
    if absent:
        return False, (
            "not delivered: " + ", ".join(absent) + " — the writable "
            "mount(s) were not on disk when the push ran, so the commits "
            "are wherever that workspace is and nothing reached the "
            f"remote. Workspace root was {root!r}.")

    detail = ", ".join(f"{r.at or '.'}:{r.detail or r.kind}" for r in refs)
    return True, detail or "nothing to push"
