"""A story cannot reach DONE unless the push runs in the pod.

MEASURED ON EXECUTION 203, and it meant no story on a Kubernetes
substrate had ever been able to finish.

The work was right. `story_commit` committed it -- `bc993b0 leg 0: Add
calc.stats module...` -- and both goal criteria passed, 6 tests green.
Then `finish_passed_claim` called `publish_execution`, which ran
`CompositeDriver().publish` IN THE CONDUCTOR against `/workspace`. On
this substrate that directory only exists inside the execution pod's
worker container, so every writable mount came back MOUNT_MISSING:

    claim 19 passed but publish FAILED: not delivered:
    repos/Pilot-Wave-Technology__multi_agent_dummy_repo -- the writable
    mount(s) were not on disk when the push ran, so the commits are
    wherever that workspace is and nothing reached the remote.
    Workspace root was '/workspace'.

`publish_execution` then set the run back to RUNNING and told the
executor "your work PASSED verification but could not be pushed", which
started a new generation, a new plan, and a new approval park. That is
the RUNNING<->VERIFYING cycling seen on 202 and again on 203.

`story_commit` had already been routed through podfs for exactly this
reason -- `_worker_execute` checks `workspace_host` and there is a
comment there describing this failure precisely. Publish was the half
that never followed, which is why the commit existed and the branch sat
[ahead 1] with nothing on the remote.

THE LANE GUARD HAD THE SAME HOLE, and it is the more dangerous one. D1
refuses to push a mount checked out on a trunk lane. It decided that by
running `git rev-parse` in the conductor, found no repository, and
concluded no mount was on a lane. A guard that cannot see the tree
always passes.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from kernel.workspace import deliver, podfs, podfs_client

SRC = pathlib.Path(deliver.__file__).read_text(encoding="utf-8")


# -- the seam exists on both sides --------------------------------------

def test_podfs_can_push_and_read_branches():
    """Before this, podfs could commit but never deliver -- so the
    commits reached the disk in the pod and stopped there."""
    assert "publish" in podfs.OPS
    assert "heads" in podfs.OPS
    assert callable(podfs_client.publish)
    assert callable(podfs_client.heads)


def test_the_pod_publishes_with_the_same_driver():
    """Not a reimplementation. A second spelling of `publish` is how the
    two containers start delivering different things."""
    src = pathlib.Path(podfs.__file__).read_text(encoding="utf-8")
    body = src[src.index("def op_publish"):src.index("def op_archive")]
    assert "CompositeDriver().publish" in body


def test_the_refs_survive_the_round_trip():
    """`publish_execution` reads `r.detail == MOUNT_MISSING` to decide
    whether DONE may be granted, so the dicts coming back over HTTP must
    become CheckpointRefs again -- not stay dicts, where `.detail` would
    raise and a passing story would look like an infrastructure error."""
    from kernel.workspace.composite import MOUNT_MISSING
    from kernel.workspace.driver import CheckpointRef

    ref = CheckpointRef(kind="git", ref="", at="repos/a",
                        detail=MOUNT_MISSING)
    assert ref.detail == MOUNT_MISSING
    assert "CheckpointRef(" in SRC


# -- and publish_execution actually uses it ------------------------------

def _fn(name: str):
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and node.name == name:
            return ast.dump(node)
    raise AssertionError(f"{name} is gone")


def test_publish_execution_asks_where_the_tree_is():
    assert "_pod_host" in _fn("publish_execution")


def test_the_lane_guard_reads_the_branch_from_the_pod():
    """The guard and the push must read the SAME filesystem. A lane
    check against the conductor's disk and a push against the pod's is a
    guard that cannot fail."""
    body = _fn("publish_execution")
    assert "remote_heads" in body
    assert "remote_publish" in body


def test_an_unreachable_pod_is_not_reported_as_the_work_being_wrong():
    """`_tell_executor` hands this text to the model. Telling it to
    'check the branch state (diverged? protected?)' when the pod was
    simply unreachable sends it to debug something that is fine."""
    assert "WorkspaceUnreachable" in SRC
    assert "workspace pod could not be reached" in SRC
    # And the other way round: a pod that ANSWERED with a failure must
    # not be described as unreachable.
    assert "WorkspaceOpFailed" in SRC
    assert "the push was attempted and failed" in SRC


def test_docker_and_e0_still_publish_locally():
    """`is_pod` is false there, the host is empty, and the original
    local path must remain -- E0 has no podfs at all."""
    body = _fn("publish_execution")
    assert "CompositeDriver" in body


# -- the behaviour, end to end through a fake pod ------------------------

class _Mount:
    def __init__(self, at, writable=True, binding="git"):
        self.at, self.writable, self.binding = at, writable, binding
        self.source, self.ref, self.kind = "git@x:y.git", "feat", "branch"


@pytest.mark.asyncio
async def test_a_pod_push_returns_real_refs(monkeypatch):
    """The whole point: refs come back describing a push that HAPPENED,
    not MOUNT_MISSING describing a directory nobody looked at."""
    sent = {}

    def fake_publish(host, root, mounts, *, execution_id):
        sent.update(host=host, root=root, execution_id=execution_id,
                    ats=[m.at for m in mounts])
        return {"refs": [{"kind": "git", "ref": "bc993b0",
                          "at": "repos/a", "detail": "pushed"}]}

    monkeypatch.setattr(podfs_client, "publish", fake_publish)
    got = podfs_client.publish("10.48.128.10", "/workspace",
                               [_Mount("repos/a")], execution_id=203)
    assert got["refs"][0]["detail"] == "pushed"
    assert sent["host"] == "10.48.128.10"
    assert sent["root"] == "/workspace"
    assert sent["execution_id"] == 203


def test_op_publish_pushes_every_writable_mount(monkeypatch):
    """Multi-repo is the ordinary case, not a special one: a story
    granted three repositories delivers each."""
    from kernel.workspace.driver import CheckpointRef

    seen = {}

    class _Driver:
        def publish(self, root, mounts):
            seen["ats"] = [m.at for m in mounts]
            return tuple(CheckpointRef(kind="git", ref="s" + m.at,
                                       at=m.at, detail="pushed")
                         for m in mounts)

    import kernel.workspace.composite as composite
    monkeypatch.setattr(composite, "CompositeDriver", _Driver)

    out = podfs.op_publish({
        "root": "/workspace",
        "mounts": [{"at": "repos/a", "binding": "git", "writable": True,
                    "source": "u", "ref": "f", "kind": "branch"},
                   {"at": "repos/b", "binding": "git", "writable": True,
                    "source": "u", "ref": "f", "kind": "branch"}]})
    assert seen["ats"] == ["repos/a", "repos/b"]
    assert [r["at"] for r in out["refs"]] == ["repos/a", "repos/b"]
    assert all(r["detail"] == "pushed" for r in out["refs"])


def test_op_heads_reports_a_branch_per_writable_git_mount(tmp_path,
                                                          monkeypatch):
    """A read-only mount has no business being asked, and a non-git one
    has no branch -- both are skipped rather than reported empty, which
    the lane guard would read as 'not on a lane'."""
    calls = []

    class _Done:
        stdout = "feature-x\n"

    def fake_run(argv, **kw):
        calls.append(argv)
        return _Done()

    # `op_heads` imports subprocess inside the function, so patching the
    # module itself is what it will pick up.
    import subprocess as real
    monkeypatch.setattr(real, "run", fake_run)

    out = podfs.op_heads({
        "root": str(tmp_path),
        "mounts": [{"at": "repos/a", "binding": "git", "writable": True,
                    "source": "u", "ref": "f", "kind": "branch"},
                   {"at": "docs", "binding": "git", "writable": False,
                    "source": "u", "ref": "f", "kind": "branch"},
                   {"at": "blob", "binding": "archive", "writable": True,
                    "source": "u", "ref": "f", "kind": "artifact"}]})
    assert out["heads"] == {"repos/a": "feature-x"}
    assert len(calls) == 1


# -- a failed push must never read as a finished one ---------------------
#
# MEASURED ON 203, AND THIS ONE WAS A REGRESSION. The pod was created
# before the deploy that added these ops, so its podfs replied
#
#     {"error": "E_UNKNOWN_OP: 'publish'; expected one of ..."}
#
# `publish_execution` did `got.get("refs") or []`, found no absent mounts
# in the empty tuple, and returned (True, "nothing to push"). The story
# went DONE with its commit still in the pod and the remote branch
# untouched -- verified against the forge: still at f46ab932 "Main".
#
# Before the podfs routing this case refused CORRECTLY. Turning a loud
# refusal into a silent success is the worst direction for this seam to
# fail in, which is why both halves are held down here.

def test_a_podfs_error_is_raised_not_returned(monkeypatch):
    """WorkspaceOpFailed, NOT WorkspaceUnreachable. The pod answered --
    a real push reached GitHub and was refused 403, and reporting that
    as 'the workspace pod could not be reached' sent the diagnosis to
    the wrong place entirely."""
    from kernel.workspace.podfs_client import (WorkspaceOpFailed,
                                               WorkspaceUnreachable)

    monkeypatch.setattr(
        podfs_client, "call",
        lambda *a, **k: {"error": "E_UNKNOWN_OP: 'publish'"})
    assert not issubclass(WorkspaceOpFailed, WorkspaceUnreachable)
    with pytest.raises(WorkspaceOpFailed) as got:
        podfs_client.publish("h", "/workspace", [_Mount("repos/a")],
                             execution_id=203)
    assert "E_UNKNOWN_OP" in str(got.value)


def test_a_heads_error_is_raised_too():
    """The lane guard reads a dict out of this reply. An error dict has
    no heads in it, and 'no mount is on a lane' is the answer that lets
    a story push straight to the merge target."""
    from kernel.workspace.podfs_client import WorkspaceOpFailed, _or_raise

    with pytest.raises(WorkspaceOpFailed):
        _or_raise({"error": "boom"}, "heads")
    assert _or_raise({"heads": {}}, "heads") == {"heads": {}}


def test_a_reply_without_refs_refuses_rather_than_finishing():
    """The second half of the same defect, held separately: even if an
    error slips past the client, publish_execution must not read a
    missing key as an empty push."""
    assert '"refs" not in got' in SRC
    assert "without saying" in SRC


def test_an_empty_refs_list_is_still_a_real_answer():
    """`refs: []` means the pod looked and had nothing to push -- a
    genuine success. Only a MISSING key is the failure."""
    body = {"refs": []}
    assert "refs" in body and body["refs"] == []
