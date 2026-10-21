"""Worker-side tool BODIES that need the executor, not just the arguments.

SPLIT OUT OF `executor.py` so that file is the routing table and this one
is the work. The division is the one `tools_worker/__init__.py` already
draws and one step further along it: a `tools_worker` function takes the
one thing it needs and nothing else — `skill` a profile, `story_commit`
an envelope — whereas each of these reaches for SEVERAL parts of the
running execution at once, the envelope and the bridge address and the
dispatch context together, which is why none of them could go there
without the signature becoming the executor spelled out longhand.

Each function takes the executor as `ex` and reads the same private
attributes the methods read, because this is a move and not a rewrite.

WHERE `_env_install` WENT, since this header used to argue it could not
leave `executor.py`. That argument was that two guards read the old
location — one `kernel/harness/executor.py` by path for
`jobs_client.run_job(`, one `inspect.getsource(
BridgeToolExecutor._env_install)` for `shlex.quote` — and moving the
code would leave them reading a file the call was no longer in. True,
and it was the wrong conclusion: wave 4 moved BOTH GUARDS with the code,
to the executor family and to the module respectively. It now lives in
`kernel/harness/executor_env_install.py`, not here, because 130 lines
plus its 74 is over the house ceiling and because it is not really a
peer of `env_setup` — `env_setup` re-applies what the committed tree
declares and records nothing, and the recording is the whole of
`env_install`.
"""

from __future__ import annotations

import asyncio
import logging

from kernel.harness.models.types import ToolResult

logger = logging.getLogger(__name__)


async def env_setup(ex, conn, tool_args: dict,
                    tool_call_id: str) -> ToolResult:
    """(Re)apply the workspace's committed setups.

    The move after editing a manifest: change requirements.txt, call
    this, and the sandbox matches what the Verifier will build from
    the same file. There is nothing to record — the environment is a
    function of the committed tree.
    """
    from kernel.worker.envsetup import run_setups

    workspace = await conn.fetchval(
        "SELECT workspace_path FROM executions WHERE id = $1",
        ex._execution_id)
    if not workspace:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="env_setup: this run has no workspace",
                          exit_code=1)
    result = await run_setups(conn, ex._execution_id, workspace,
                              ex._port, ex._host)
    if result is None:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error="env_setup: no setup found. Commit a manifest "
                  "(requirements.txt, package-lock.json) or an "
                  ".agentos/setup.sh at the mount root first.",
            exit_code=1)
    if result["state"] == "running":
        # E14 — NOT AN ANSWER YET, AND NOT A FAILURE. The install is
        # a job in the sandbox under its own budget; this pass
        # watched it for as long as it was willing to. Calling
        # env_setup again re-attaches to the same job rather than
        # starting a second install against the same tree.
        return ToolResult(
            tool_call_id=tool_call_id, error="", exit_code=0,
            output=result["output"] + "\n\nStill installing. Call "
                   "env_setup again to check on it — the same job is "
                   "picked back up, never restarted.")
    return ToolResult(tool_call_id=tool_call_id,
                      output=result["output"], error="",
                      exit_code=0 if result["ok"] else 1)


async def remote_commit(ex, host: str, tool_args: dict,
                        tool_call_id: str) -> ToolResult:
    """`story_commit`, RUN WHERE THE REPOSITORIES ARE.

    `story_commit` shells out to git in every writable mount; on a pod
    substrate those are in the pod and the worker process would
    `git add -A` in a directory that does not exist -- reported as
    "nothing to commit", which is the answer a story with real work
    would never get and nobody would question.
    """
    from kernel.workspace.podfs_client import (
        WorkspaceUnreachable, commit as _remote_commit)

    try:
        # OFF THE LOOP: a synchronous urlopen, and a commit
        # across several repos is not quick.
        out = await asyncio.to_thread(
            _remote_commit,
            host, ex._envelope.workspace_path or "",
            ex._envelope.mounts or (), tool_args, tool_call_id,
            # Off the envelope, not off the executor: the envelope
            # is what a sub-agent inherits, and podfs
            # authenticates per execution.
            execution_id=ex._envelope.execution_id)
    except WorkspaceUnreachable as exc:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error=f"E_WORKSPACE_UNREACHABLE: {exc}",
                          exit_code=1)
    return ToolResult(tool_call_id=tool_call_id,
                      output=out.get("output") or "",
                      error=out.get("error"),
                      exit_code=int(out.get("exit_code") or 0))


async def goal_knowledge(ex, conn, tool_args: dict,
                         tool_call_id: str) -> ToolResult:
    """What this goal already settled, read off the goal's own record."""
    from kernel.knowledge.read import goal_knowledge as _gk
    row = await conn.fetchrow(
        "SELECT goal_id FROM executions WHERE id = $1",
        ex._execution_id)
    if row is None:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="goal_knowledge: no such run",
                          exit_code=1)
    import json as _json
    out = await _gk(conn, row["goal_id"],
                    story=str(tool_args.get("story") or ""),
                    about=str(tool_args.get("about") or ""),
                    cycle=int(tool_args.get("cycle") or 0),
                    limit=int(tool_args.get("limit") or 25))
    return ToolResult(tool_call_id=tool_call_id,
                      output=_json.dumps(out, default=str, indent=2),
                      exit_code=0)


async def dispatch_subagent(ex, tool_args: dict,
                            tool_call_id: str) -> ToolResult:
    """Bound a child and run it, or refuse for want of the bounds."""
    if ex._subagent_context is None or "adapter" not in ex._subagent_context:
        # Refuse rather than improvise. Dispatching needs the
        # parent's depth and remaining budget; guessing either
        # would either allow unbounded recursion or hand the child
        # a budget the parent has already spent.
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error="subagent: this executor was built without a "
                  "dispatch context, so no child can be bounded",
            exit_code=1)
    ctx = ex._subagent_context
    from kernel.harness.subagent_run import subagent as _subagent
    return await _subagent(
        parent_envelope=ex._envelope,
        parent_profile=ex._profile,
        conn=ctx["conn"], adapter=ctx["adapter"],
        execution_id=ex._execution_id,
        generation=ex._envelope.generation,
        actor_id=ctx["actor_id"],
        parent_depth=ctx["depth"],
        parent_budget_remaining=ctx["budget_remaining"](),
        bridge_port=ex._port,
        bridge_host=ex._host,
        tool_args=tool_args, tool_call_id=tool_call_id)


__all__ = ["dispatch_subagent", "env_setup", "goal_knowledge",
           "remote_commit"]


async def remote_sync(ex, conn, tool_call_id: str) -> ToolResult:
    """`sync_trunk`, RUN WHERE THE CREDENTIAL IS.

    THE DIFF STORY'S GIT HALF. A feature branch is cut from a trunk that
    then moves, and the branch has to take those changes before it can
    merge. The engine cannot do it: its GitHub permissions are read-only
    by design -- "detection never writes to GitHub (the human merges)",
    and the write merge ops are retired from the live path -- so the
    merge happens in a workspace, as an ordinary coding story.

    AND THE MODEL CANNOT DO IT EITHER, which is the part that is not
    obvious. `git fetch` needs a credential; the helper is persisted
    into `.git/config` where the sandbox can read it, but it is a
    POINTER -- the bearer sits in a worker-private directory the sandbox
    has no mount for. A `bash` fetch therefore authenticates as nobody
    and fails on a private repository. Commit and publish are worker-
    side for this reason; this is the third of the same kind.

    THE LANE COMES FROM THE RECORD, not from the model. Which branch a
    story merges into is a fact about the goal (`goal_resources`, role
    `trunk`), and asking the model for it would let a story merge
    whatever it named.
    """
    from kernel.harness.sync_lanes import lanes_for_mounts
    from kernel.workspace.mounts import mounts_for
    from kernel.workspace.podfs_client import (WorkspaceUnreachable,
                                               sync as _remote_sync)

    execution_id = ex._envelope.execution_id
    mounts = await mounts_for(conn, execution_id)
    lanes = await lanes_for_mounts(conn, execution_id)
    if not lanes:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error="E_NO_TRUNK_LANE: none of this story's writable repos "
                  "has a trunk lane to merge from -- either the goal "
                  "registers no trunk branch for them, or each one is "
                  "already checked out ON its lane",
            exit_code=1)

    host = getattr(ex._envelope, "workspace_host", "") or ""
    # WHAT IT WAS ASKED TO DO, BEFORE IT TRIES. A sync that fails used
    # to leave no trace anywhere: the ToolResult goes to the MODEL,
    # `step_journal.result_ref` is null for this tool, and nothing
    # logged. Two diff stories on dev (executions 47 and 48,
    # 2026-10-07) called `sync_trunk` once, got nothing usable, and
    # spent the rest of the run doing the merge by hand -- and the only
    # way to tell was to read 50 bash calls and infer it. Whoever looks
    # next should be able to read the answer instead.
    logger.info("execution %s: sync_trunk host=%r root=%r lanes=%s",
                execution_id, host or "(local)",
                ex._envelope.workspace_path or "", lanes)
    try:
        if host:
            out = await asyncio.to_thread(
                _remote_sync, host, ex._envelope.workspace_path or "",
                mounts, lanes,
                execution_id=execution_id)
        else:
            # NO HOST MEANS THE TREE IS HERE, NOT THAT IT IS MISSING.
            #
            # `story_commit` has always made this distinction
            # (`executor_worker_dispatch.worker_execute`): with a host
            # it crosses into the pod, without one it runs the local
            # path, because an empty `workspace_host` is exactly what
            # an execution whose tree is on THIS filesystem looks like
            # -- which is every leg running inside its own pod, and
            # every Docker and E0 run.
            #
            # `sync_trunk` never had it. It always went through
            # `podfs_client`, which raises WorkspaceUnreachable on an
            # empty host -- "Running it here would read the conductor's
            # own filesystem", true on the conductor and false in the
            # pod. So the one tool a diff story exists to call failed
            # on EVERY diff story, and the model quietly did the merge
            # with raw git instead: the outcome looked right, the
            # branch landed, and nothing said the platform's own tool
            # had not done it.
            #
            # Measured on dev 2026-10-07, executions 47, 48 and 51 --
            # 51 twice, after being steered to use the tool explicitly.
            from kernel.workspace.podfs import op_sync    # noqa: PLC0415
            from kernel.workspace.podfs_client import _rows  # noqa: PLC0415

            logger.info("execution %s: sync_trunk runs in-process; the "
                        "tree is on this filesystem", execution_id)
            out = await asyncio.to_thread(
                op_sync,
                {"root": ex._envelope.workspace_path or "",
                 "mounts": _rows(mounts), "lanes": lanes,
                 "execution_id": execution_id})
    except WorkspaceUnreachable as exc:
        logger.warning("execution %s: sync_trunk UNREACHABLE: %s",
                       execution_id, exc)
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error=f"E_WORKSPACE_UNREACHABLE: {exc}",
                          exit_code=1)
    synced = out.get("synced") or {}
    # PER MOUNT, AND THE DETAIL ON A FAILURE. "error" here is the whole
    # diagnosis -- `_sync_one` puts git's own stderr in it -- and it was
    # being handed to the model and dropped.
    for at, got in sorted(synced.items()):
        state = got.get("state")
        if state in ("merged", "already"):
            logger.info("execution %s: sync_trunk %s: %s (behind=%s)",
                        execution_id, at, state, got.get("behind", 0))
        else:
            logger.warning("execution %s: sync_trunk %s: %s -- %s",
                           execution_id, at, state,
                           (got.get("detail") or "")[:400])
    if not synced:
        logger.warning("execution %s: sync_trunk returned NO mounts at "
                       "all (reply keys: %s)", execution_id,
                       ", ".join(sorted(out)) or "(empty)")

    # WHO WRITES DOWN THAT IT SYNCED. Not this process, and that is
    # the correction. `git_nodes.last_synced_trunk_sha` answers "is
    # this branch behind trunk", and a pod tried to write it here --
    # every sync since logged `permission denied for table git_nodes`,
    # because a leg runs as `agentos_leg_dev` and has no write on the
    # git graph.
    #
    # A GRANT WOULD BE THE WRONG FIX. An executor that can set its own
    # `last_synced_trunk_sha` can declare itself caught up without
    # merging anything, and never be offered integration again -- it
    # would land un-integrated work and the platform would agree it
    # was fine. Blocked executors have reached for credentials before;
    # this is not a lock to hand them the key to.
    #
    # The engine already knows the answer first-hand: `grind` compares
    # the branch against trunk on GitHub every pass, and `behind_by ==
    # 0` is evidence, where a pod's self-report is only a claim. So the
    # column is written there -- see `record_caught_up` in
    # app/services/trunk_integration.py.
    return _sync_result(synced, tool_call_id)


def _sync_result(synced: dict, tool_call_id: str) -> ToolResult:
    """What the model is told, and a conflict is NOT an error.

    A conflict means the merge ran and found two real answers -- the one
    thing here the model CAN finish, because resolving it is editing
    files and needs no credential. Reporting it as a failed tool call
    would send it looking for a broken tool instead of at the markers
    sitting in its tree.
    """
    lines, failed = [], []
    for at, got in sorted(synced.items()):
        state = got.get("state")
        if state == "already":
            lines.append(f"{at}: already up to date with the trunk")
        elif state == "merged":
            lines.append(f"{at}: merged {got.get('behind', 0)} trunk "
                         f"commit(s) cleanly -- commit this with "
                         f"story_commit")
        elif state == "conflict":
            paths = got.get("conflicts") or []
            lines.append(
                f"{at}: CONFLICT in {len(paths)} file(s) after taking "
                f"{got.get('behind', 0)} trunk commit(s). Resolve the "
                f"markers, then story_commit:\n  "
                + "\n  ".join(paths))
        else:
            failed.append(f"{at}: {got.get('detail') or state}")
    if failed and not lines:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="E_SYNC_FAILED: " + "; ".join(failed),
                          exit_code=1)
    if failed:
        lines.append("could not sync: " + "; ".join(failed))
    # A7's ROUTINE INVALIDATOR, said out loud. A sync that took commits
    # moved the tree, so every check recorded against the old one stops
    # applying -- `run_check` will re-run rather than reuse, and the
    # model should expect that rather than read it as a tool misfiring.
    # A sync that took nothing changed nothing, and the recorded runs
    # stand.
    if any((got.get("state") in ("merged", "conflict"))
           for got in synced.values()):
        lines.append("the tree has moved, so any check already run "
                     "against it will run again")
    return ToolResult(tool_call_id=tool_call_id, output="\n".join(lines),
                      error=None, exit_code=0)
