"""The one tool a diff story exists to call, and why it was never called.

TWO FAULTS THAT HID EACH OTHER, both measured on dev 2026-10-07
(executions 47, 48 and 51).

1. `sync_trunk` FAILED ON EVERY EXECUTION, with
   `E_WORKSPACE_UNREACHABLE: no address for the pod holding this
   workspace`. `remote_sync` always went through `podfs_client`, which
   raises on an empty `workspace_host` -- and a leg running INSIDE its
   own pod has an empty one, because the tree is on its own
   filesystem. `story_commit` has always made that distinction
   (`executor_worker_dispatch.worker_execute`: `if host:` remote, else
   local) and worked fine on the same executions. `sync_trunk` never
   had it, so it could not succeed anywhere legs run in the pod.

2. THE BRIEF TOLD THE MODEL TO USE RAW GIT ANYWAY. `_ensure_diff_story`
   spelled out `git pull origin <trunk>` as step 1 and `git push
   origin <branch>` as step 5. The model followed it exactly -- run
   51's approved plan was "Pull trunk changes ... via git pull origin
   bmi_backend".

So the tool was broken AND unused, and the second hid the first: the
manual merge worked, the branch landed, the checks passed, and nothing
anywhere recorded that the platform's own tool had not done it. It
took reading `step_journal` to notice.

WHY THE PUSH MATTERS TOO. The brief's step 5 had diff stories pushing
their own branch, which no other story does: `propose_completion` and
the harness publish. One shape for every story is the point.
"""

from __future__ import annotations

import inspect

import pytest

from app.services.git_graph import grind
from kernel.harness import executor_tools


# ── 1. the tool works where legs actually run ────────────────────────

def test_sync_runs_in_process_when_there_is_no_pod_host():
    """An empty host means the tree is HERE, not that it is missing --
    which is every leg running inside its own pod."""
    src = inspect.getsource(executor_tools.remote_sync)

    assert "if host:" in src, (
        "sync_trunk does not branch on the host, so it raises "
        "WorkspaceUnreachable on every leg that runs in its own pod")
    local = src[src.index("else:"):]
    assert "op_sync" in local, (
        "the no-host branch does not run the sync locally")


def test_it_takes_the_same_branch_story_commit_does():
    """The precedent, and the reason this is a fix and not a new idea:
    one tool had the distinction and the other did not."""
    from kernel.harness import executor_worker_dispatch

    commit = inspect.getsource(executor_worker_dispatch.worker_execute)
    assert "if host:" in commit, (
        "story_commit no longer branches on the host; if that changed "
        "deliberately, sync_trunk should follow it")


def test_the_local_path_is_the_same_op_the_pod_would_run():
    """Not a reimplementation. `op_sync` is what podfs serves, so the
    in-pod and cross-pod paths cannot drift apart."""
    src = inspect.getsource(executor_tools.remote_sync)
    assert "from kernel.workspace.podfs import op_sync" in src
    assert "_rows(mounts)" in src, (
        "mounts must be serialised exactly as podfs_client sends them, "
        "or the local path sees a different workspace")


# ── 2. the brief names the tool ──────────────────────────────────────

def test_the_brief_tells_the_model_to_use_sync_trunk():
    src = inspect.getsource(grind._ensure_diff_story)
    assert "sync_trunk" in src, (
        "the brief never names the tool, so the model reaches for git")


def test_the_brief_no_longer_prescribes_a_manual_pull():
    """It used to say `git pull origin {trunk}` as step 1, and run 51's
    plan echoed it almost word for word."""
    src = inspect.getsource(grind._ensure_diff_story)
    step_one = src[src.index('f"1.'):src.index('f"2.')]
    assert "git pull origin {trunk}" not in step_one.replace(
        "used to spell out `git pull origin {trunk}`", ""), (
        "step 1 still prescribes a manual pull")
    assert "Do NOT use `git pull`" in src


def test_the_brief_does_not_tell_it_to_push():
    """A diff story finishes like every other story: story_commit, then
    propose_completion, and the harness publishes."""
    src = inspect.getsource(grind._ensure_diff_story)
    assert "git push origin {branch}" not in src, (
        "the brief still instructs a manual push")
    assert "story_commit" in src and "propose_completion" in src
    assert "Do NOT " in src and "git push" in src


def test_a_conflict_is_still_described_as_work_not_failure():
    """Carried over from the tool's own contract -- resolving markers
    needs no credential and is the part the model CAN finish."""
    src = inspect.getsource(grind._ensure_diff_story)
    assert "CONFLICT" in src
    assert "not a failure" in src


def test_the_reconcile_step_survived_the_rewrite():
    """The valuable half of the brief: a clean text merge can still be
    semantically broken."""
    src = inspect.getsource(grind._ensure_diff_story)
    assert "RECONCILE" in src
    assert "EVEN IF the merge" in src


# ── 3. and it actually merges, through the real entry point ─────────

@pytest.mark.asyncio
async def test_sync_trunk_merges_with_no_host_against_real_git(tmp_path):
    """END TO END through `remote_sync`, not through `op_sync`.

    The two tests above assert the branch EXISTS. This one proves it
    works: an execution whose `workspace_host` is empty -- every leg
    running inside its own pod -- must come back `merged`, not
    `E_WORKSPACE_UNREACHABLE`, which is what executions 47, 48 and 51
    each got.
    """
    import subprocess
    from kernel.workspace.mounts import Mount

    def git(cwd, *a):
        out = subprocess.run(["git", "-C", str(cwd), *a],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    origin = tmp_path / "origin.git"
    work = tmp_path / "build"
    work.mkdir()
    git(work, "init", "-q", "-b", "trunk")
    git(work, "config", "user.email", "t@t.invalid")
    git(work, "config", "user.name", "t")
    (work / "README").write_text("base\n", encoding="utf-8")
    git(work, "add", "-A"); git(work, "commit", "-qm", "base")
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(origin)],
                   check=True)

    root = tmp_path / "ws"
    at = "repos/acme__app"
    dest = root / "repos" / "acme__app"
    dest.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "-b", "trunk", str(origin),
                    str(dest)], check=True)
    git(dest, "config", "user.email", "t@t.invalid")
    git(dest, "config", "user.name", "t")
    git(dest, "checkout", "-q", "-b", "feat/x")

    # The trunk moves after the branch was cut -- the diff story's case.
    git(work, "checkout", "-q", "trunk")
    (work / "FROM_TRUNK").write_text("moved\n", encoding="utf-8")
    git(work, "add", "-A"); git(work, "commit", "-qm", "trunk moves")
    git(work, "push", "-q", str(origin), "trunk")

    class _Env:
        workspace_host = ""          # the whole point
        workspace_path = str(root)
        execution_id = 1
        mounts = ()

    class _Ex:
        _envelope = _Env()

    class _Conn:
        async def fetch(self, sql, *a):
            if "gr.kind = 'branch'" in sql and "em.at" in sql:
                return [{"at": at, "ref": "feat/x", "lane": "trunk",
                         "repo_owner": "acme", "repo_name": "app"}]
            return []

    async def _mounts_for(conn, eid):
        return (Mount(at=at, binding="git", writable=True, kind="repo"),)

    import kernel.workspace.mounts as m
    orig = m.mounts_for
    m.mounts_for = _mounts_for
    try:
        got = await executor_tools.remote_sync(_Ex(), _Conn(), "tc1")
    finally:
        m.mounts_for = orig

    assert got.exit_code == 0, got.error
    assert "merged" in (got.output or ""), got.output
    assert (dest / "FROM_TRUNK").exists(), (
        "the trunk commit did not reach the working tree")


# ── 4. and the executor's own prompt teaches the shape ──────────────

def test_the_prompt_teaches_the_integration_shape():
    """THE STORY THE ENGINE USED TO CUT IS NOW THE STORY'S OWN WORK.

    This asserted the prompt had a section headed "When the whole story
    IS the integration", because the engine cut a separate diff story to
    merge the lane in. It did that because the feature story was already
    `done` by then and its workspace collected — there was nowhere left
    to work. That stopped being true when a coding story began holding
    its pod until its work lands, so the heading is deliberately gone.

    WHAT IS NOT GONE is everything executions 47, 48 and 51 taught,
    which is about the TOOL and not about who owns the story. Those are
    pinned below unchanged.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    body = (root / "profiles" / "story-coding" / "prompt.md").read_text(
        encoding="utf-8")

    assert "Trunk moving under you is now part of YOUR story" in body
    assert "When the whole story IS the integration" not in body, (
        "the prompt still describes the separately-cut diff story; an "
        "executor told to wait for one will wait for a story the engine "
        "no longer makes")

    # the four things that went wrong in executions 47, 48 and 51
    assert "Do not `git push`" in body, "it still may push its own branch"
    assert "stop and say so" in body, (
        "nothing tells it to surface a sync_trunk error rather than "
        "reproducing the merge by hand, which is what hid the bug")
    assert "propose_completion" in body
    assert "Each writable clone syncs its own lane" in body, (
        "multi-repo is not covered")


def test_the_prompt_says_what_the_scoped_approval_does_not_cover():
    """The canned plan is approved by nobody. An executor that does not
    know it is scoped reads the approval as permission to proceed
    through a conflict, and spends its legs being refused."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    body = (root / "profiles" / "story-coding" / "prompt.md").read_text(
        encoding="utf-8")

    assert "SCOPED" in body
    assert "register_plan" in body
    for case in ("conflicts", "fails after it"):
        assert case in body, (
            f"the prompt does not name {case!r} as a stop-and-ask case")
