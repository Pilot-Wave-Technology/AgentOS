"""Work on the tree happens where the tree is.

THIS ALL WORKED ON DOCKER, and that is the whole shape of the bug.

The leg runs in the CONDUCTOR. On E0 the conductor shares the workspace
volume, so `open('/workspace/repos/.../agentos_plans/...')` landed in
the story's checkout and the plan rode its commits into the PR. On
Kubernetes the tree lives in the execution's own pod and the conductor
has no such path, so on NCAL4 runs 191 and 192:

    plan file for 191 not written under repos/...:
    [Errno 13] Permission denied: '/workspace'

    dryrun: no commit at root for execution 192 (git rev-parse HEAD
    failed in /workspace: cannot change to '/workspace'); trying the
    mounts

-- and "trying the mounts" read the same absent filesystem, so the
fallback was no fallback. Consequences, both silent:

  the plan stopped being mirrored into the repo at all, which is the
  structure -- a plan that lives only in a database row is invisible at
  the one place the work is reviewed

  every dry-run concluded "no committed tree to check against yet",
  while the park message told the person the checks "are being run
  against the current tree to confirm they can tell the difference".
  They were not being run against anything.

THE ANSWER `story_commit` ALREADY USED. `podfs.op_commit` does not
reimplement committing; it reconstructs the envelope in the pod and
calls the same `tools_worker.story_commit`. So `op_plan` calls the same
`planfiles.write_files` and `op_digest` the same `mount_digest` /
`head_commit`. One implementation, two places it can run from.
"""
from __future__ import annotations

import inspect

import pytest

from kernel.harness import planfiles
from kernel.workspace import podfs, podfs_client

PLAN_BODY = "# the plan" + chr(10)


# ── the plan reaches the repo ────────────────────────────────────────

def test_the_file_writing_is_one_function():
    """Not "the pod has its own copy of what a plan file is"."""
    assert callable(planfiles.write_files)
    assert "write_files" in inspect.getsource(podfs.op_plan)


def test_a_local_tree_is_still_written_directly():
    """Docker shares the volume; crossing a network for a local file
    would be slower and one more thing to fail.

    RE-POINTED, and the literal it used to pin is the bug it was
    pinning. It asserted `if not is_pod(resolve()):` -- the exact
    expression -- and `is_pod` answers "is the tree in a pod", which is
    NOT the question this branch asks. A leg inside that pod reads True
    and crosses to a filesystem mounted in its own container, by way of
    a Kubernetes API call it has no Role to make. See
    test_which_side_of_the_pod_boundary.py.

    The property this test is actually about -- a local tree is written
    directly -- is unchanged and now true in one more place.
    """
    src = inspect.getsource(planfiles.persist_plan)
    assert "if not tree_is_remote():" in src
    assert "return write_files(" in src


def test_a_pod_tree_is_written_through_podfs():
    src = inspect.getsource(planfiles.persist_plan)
    assert "remote_plan" in src
    assert "workspace_host" in src, (
        "the address comes from where dispatch actually resolved it")


def test_the_blocking_call_leaves_the_event_loop():
    """`podfs_client` is blocking urllib; called inline it stalls the
    conductor's loop for the length of a pod round-trip."""
    assert "asyncio.to_thread" in inspect.getsource(planfiles.persist_plan)


def test_an_unreachable_workspace_does_not_fail_the_plan():
    """The registration is the plan; the file is a courtesy copy, and
    the caller says so: "a workspace that cannot be written must not
    block an approval on a courtesy copy"."""
    src = inspect.getsource(planfiles.persist_plan)
    assert "WorkspaceUnreachable" in src and "return []" in src


def test_the_client_sends_what_the_op_reads():
    """The two halves of one wire format, which is exactly the kind of
    thing that drifts."""
    sent = inspect.getsource(podfs_client.plan)
    read = inspect.getsource(podfs.op_plan)
    for field in ("root", "mounts", "story", "cycle", "body"):
        assert field in sent, f"client omits {field}"
        assert field in read, f"op ignores {field}"


# ── and it actually writes files, not just the right spelling ────────

def test_op_plan_writes_into_every_writable_git_mount(tmp_path):
    """The behaviour. A guard that greps for a function name passes for
    a function that does nothing."""
    (tmp_path / "repos" / "acme__one").mkdir(parents=True)
    (tmp_path / "repos" / "acme__two").mkdir(parents=True)
    (tmp_path / "docs").mkdir()

    out = podfs.op_plan({
        "root": str(tmp_path),
        "mounts": [
            {"at": "repos/acme__one", "binding": "git", "writable": True},
            {"at": "repos/acme__two", "binding": "git", "writable": True},
            # read-only, and a document: neither is a place a plan goes
            {"at": "repos/acme__ro", "binding": "git", "writable": False},
            {"at": "docs", "binding": "copyin", "writable": True},
        ],
        "story": "ca6a41b2", "cycle": 2, "body": PLAN_BODY})

    written = out["result"]
    assert sorted(written) == [
        "repos/acme__one/agentos_plans/ca6a41b2/plan-02.md",
        "repos/acme__two/agentos_plans/ca6a41b2/plan-02.md"]
    for rel in written:
        assert (tmp_path / rel).read_text(encoding="utf-8") == PLAN_BODY
    assert not (tmp_path / "docs" / "agentos_plans").exists()


def test_one_unwritable_mount_does_not_lose_the_others(tmp_path):
    """A degraded mirror beats no mirror: the plan belongs in whichever
    repos can take it."""
    (tmp_path / "repos" / "ok").mkdir(parents=True)
    # `bad` is a FILE where a directory must go, so makedirs raises
    (tmp_path / "repos").mkdir(exist_ok=True)
    (tmp_path / "repos" / "bad").write_text("not a directory",
                                            encoding="utf-8")

    out = podfs.op_plan({
        "root": str(tmp_path),
        "mounts": [{"at": "repos/bad", "binding": "git", "writable": True},
                   {"at": "repos/ok", "binding": "git", "writable": True}],
        "story": "s", "cycle": 1, "body": "x"})
    assert out["result"] == ["repos/ok/agentos_plans/s/plan-01.md"]


def test_op_digest_reports_a_real_commit(tmp_path):
    """Verification's whole question: which tree would these checks run
    against."""
    import subprocess

    repo = tmp_path / "repos" / "acme__one"
    repo.mkdir(parents=True)
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "README.md").write_text("hello", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "first")

    out = podfs.op_digest({
        "root": str(tmp_path),
        "mounts": [{"at": "repos/acme__one", "binding": "git",
                    "writable": True}]})["result"]
    shas = [sha for _, sha in out["digest"] if sha]
    assert shas and len(shas[0]) >= 7, (
        "no sha means the caller falls through to 'no committed tree "
        "to check against yet', which is what the dry run always said")


# ── and the cloud lease reaches the workspace ────────────────────────

def test_the_lease_is_written_where_the_workspace_is():
    """`issue` wrote the lease and its sidecars with plain filesystem
    calls from the conductor. On a pod there is no `/workspace` there,
    so a goal with a cloud grant silently got no identity -- which
    `issue`'s own docstring calls fatal: "a run that should have had an
    identity and silently did not fails later, somewhere unrelated, with
    a permission error nobody traces back to this decision"."""
    from kernel.credentials import issue as issue_mod

    src = inspect.getsource(issue_mod._place)
    # `tree_is_remote`, not `is_pod` -- same re-pointing, same reason as
    # `test_a_local_tree_is_still_written_directly` above.
    assert "tree_is_remote" in src and "remote_place" in src
    assert "asyncio.to_thread" in src


def test_the_lease_and_its_sidecars_go_in_one_crossing():
    """Two round-trips for one identity is two chances to half-write
    it."""
    from kernel.workspace import credentials as wc

    assert callable(wc.place_lease)
    assert "place_lease" in inspect.getsource(podfs.op_credentials)


def test_the_sweep_reaches_the_pod_too():
    """The sweep IS the release path -- "the narrow sweep IS the release
    path now" -- and it was reading the conductor's disk, so it found
    nothing and released nothing, every tick."""
    from kernel.credentials import sweep

    src = inspect.getsource(sweep)
    assert "_release_in_pod" in src and "is_pod" in src


@pytest.mark.asyncio
async def test_a_pod_that_is_already_gone_is_not_an_error(monkeypatch):
    """The ordinary case at the end of a run: the filesystem went with
    it, and the claim is the reclaim sweep's to take. A cleanup that
    raises turns every finished cloud run into a failure."""
    from kernel.credentials.sweep import _release_in_pod
    from kernel.workspace.podfs_client import WorkspaceUnreachable

    async def _no_host(conn, eid, **kw):
        raise WorkspaceUnreachable("this run has no pod")

    monkeypatch.setattr("kernel.workspace.browse.workspace_host", _no_host)
    assert await _release_in_pod(
        None, {"id": 9, "workspace_path": "/workspace"}) is False


def test_place_lease_writes_the_lease_and_every_sidecar(tmp_path):
    """The behaviour, in one call, wherever it runs."""
    from kernel.workspace.credentials import place_lease, lease_path

    written = place_lease(str(tmp_path), "exec-9-cloud",
                          {"cloud": "gcp", "account": "a@b"},
                          {".token": "ya29.secret"})
    assert lease_path(str(tmp_path), "exec-9-cloud") in written
    assert len(written) == 2
    body = [p for p in written if p.endswith(".token")][0]
    with open(body, encoding="utf-8") as fh:
        assert fh.read() == "ya29.secret"


def test_op_credentials_puts_them_on_disk(tmp_path):
    out = podfs.op_credentials({
        "root": str(tmp_path), "lease_id": "exec-9-cloud",
        "payload": {"cloud": "gcp"}, "sidecars": {".token": "t"}})
    assert len(out["result"]) == 2
    assert podfs.op_revoke_credentials(
        {"root": str(tmp_path), "lease_id": "exec-9-cloud"})["result"] is True


# ── verification runs against a real tree ────────────────────────────

def test_one_function_answers_where_the_tree_is():
    """Every caller asks it. Two callers asking the same question in two
    ways is how they come to disagree about whether a story passed. The
    dry run was the second caller here until E9 (plan 5.6); the
    adjudicator is the one that survived, and it is the one whose answer
    a verdict rests on."""
    from kernel.verifier import adjudicator, tree

    assert "tree_state" in inspect.getsource(adjudicator)
    src = inspect.getsource(tree.tree_state)
    assert "remote_digest" in src and "is_pod" in src


def test_it_still_reads_a_local_tree_directly():
    from kernel.verifier import tree

    src = inspect.getsource(tree.tree_state)
    assert "head_commit(root)" in src
    assert "mount_digest(root, mounts)" in src


def test_it_says_so_rather_than_reporting_an_empty_tree():
    """An unreachable workspace and an uncommitted one are different
    facts, and reporting the first as the second is how this hid: the
    adjudicator said "no committed tree to verify" about work that had
    committed."""
    from kernel.verifier import tree

    assert "unreachable" in inspect.getsource(tree.tree_state).lower()


def test_the_whole_workspace_can_be_archived_from_the_pod():
    """`archive_in_pod` refused any nested mount, and on Kubernetes every
    workspace has one -- so no story could reach a verdict at all."""
    from kernel.verifier import tree

    src = inspect.getsource(tree.archive_in_pod)
    assert "remote_mounts" in src
    assert "several mounts" not in src, "the blanket refusal is still there"
    assert "inject" not in inspect.getsource(
        __import__("kernel.workspace.podfs", fromlist=["x"]).op_archive_mounts
    ).split('"""')[2], "the held-out set must not cross to the executor's pod"


def test_the_digest_op_returns_both_halves():
    src = inspect.getsource(podfs.op_digest)
    assert "head_commit" in src and "mount_digest" in src
    assert '"head"' in src and '"digest"' in src


def test_both_ops_are_registered():
    assert "plan" in podfs.OPS and "digest" in podfs.OPS
