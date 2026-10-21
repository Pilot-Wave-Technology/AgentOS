"""`story_commit` — the only thing that makes work survive a leg.

E7e made a story more than one repo, so this commits in EVERY writable
mount that has changes and reports per-repo. Committing only the first
one would lose the rest silently, which is the worst shape a data-loss
bug can take: the tool answers success.
"""
from __future__ import annotations

import os
import subprocess

from kernel.harness.models.types import ToolResult

def _commit_one(path: str, message: str, env: dict):
    """Commit in ONE tree. Returns (sha, error) -- exactly one is set."""
    add = subprocess.run(["git", "add", "-A"], cwd=path,
                         capture_output=True, env=env)
    if add.returncode != 0:
        return None, add.stderr.decode(errors="replace").strip()
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                            cwd=path, capture_output=True, env=env)
    if staged.returncode == 0:
        return None, None                      # nothing to commit: not an error
    done = subprocess.run(["git", "commit", "-m", message], cwd=path,
                          capture_output=True, env=env)
    if done.returncode != 0:
        return None, done.stderr.decode(errors="replace").strip()
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                         capture_output=True, env=env)
    return sha.stdout.decode().strip(), None


def story_commit(envelope, tool_args: dict, tool_call_id: str) -> ToolResult:
    """Commit the work, in every writable mount that has changes.

    E7e — the story worktree is no longer one repo. A story granted three
    repositories commits in each one that changed, and the result is a
    SET of shas rather than a sha. N commits across N remotes cannot be
    atomic, so reporting one and hiding the rest would put a lie in the
    audit trail at the point it matters most.

    Runs on the worker side, where git credentials live (rule 9) and
    where the sandbox cannot reach them. Never raises: every failure is a
    ToolResult with exit_code 1.

    READ-ONLY MOUNTS ARE NOT CANDIDATES. A doc mounted as input has no
    remote and no business carrying a commit, and `git add -A` inside one
    would silently make it dirty for every later checkpoint.
    """
    workspace = envelope.workspace_path
    if not workspace:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error="story_commit: workspace_path is not set in the execution "
                  "envelope.",
            exit_code=1)
    if not os.path.isdir(workspace):
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"story_commit: workspace_path does not exist: "
                  f"{workspace!r}",
            exit_code=1)

    # WHO, passed on these subprocesses. Never written into .git/config:
    # that is shared by every story in the workspace and sits on disk
    # inside the tree the sandbox can read. See kernel/harness/identity.py
    # for why the human is the author and the agents are co-authors.
    identity = getattr(envelope, "identity", None)
    env = {**os.environ, **(identity.env() if identity else {})}

    message = f"leg {envelope.generation}: "               f"{tool_args.get('message', 'checkpoint')}"
    if identity:
        message = identity.message(message)

    # No mounts recorded is the single-repo story: the root IS the repo.
    # That is the shape every execution created before E7e has, and the
    # console still starts stories that way.
    mounts = [m for m in (getattr(envelope, "mounts", ()) or ())
              if getattr(m, "writable", False)]
    targets = ([(m.at, os.path.join(workspace, m.at) if m.at else workspace)
                for m in mounts] or [("", workspace)])

    committed, skipped, failures = [], [], []
    for at, path in targets:
        label = at or "<root>"
        if not os.path.isdir(path):
            failures.append(f"{label}: the mount is not present")
            continue
        sha, err = _commit_one(path, message, env)
        if err:
            failures.append(f"{label}: {err}")
        elif sha:
            committed.append((label, sha))
        else:
            skipped.append(label)

    if failures:
        return ToolResult(
            tool_call_id=tool_call_id,
            output=chr(10).join(f"{a} {s}" for a, s in committed),
            error="git commit failed: " + "; ".join(failures),
            exit_code=1)
    if not committed:
        # Said in full: which trees were looked at matters, because
        # "nothing changed anywhere" and "the one repo I edited was not
        # writable" are different problems with the same symptom.
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error="nothing to commit" + (
                f" (looked in: {', '.join(skipped)})" if skipped else ""),
            exit_code=1)

    # One line per mount. A single-repo story still gets a bare sha as
    # its first line, so callers that read one did not become wrong.
    body = chr(10).join(s if a == "<root>" else f"{a} {s}"
                        for a, s in committed)
    return ToolResult(tool_call_id=tool_call_id, output=body, exit_code=0)
