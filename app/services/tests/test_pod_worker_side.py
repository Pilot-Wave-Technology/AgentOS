"""Worker-side work runs where the workspace is — and nowhere else.

`story_commit` shells out to git in every writable mount; `emit_artifact`
reads a workspace-relative path. Both are worker-side "where git
credentials live (rule 9)", and on Docker worker-side is the conductor
process, which shares a filesystem with the sandbox. On Kubernetes it
does not: the claim is ReadWriteOncePod and belongs to the pod.

THE FAILURE THIS GUARDS IS NOT AN ERROR. Run locally against a pod's
workspace, `git add -A` finds an empty directory and reports "nothing to
commit"; `read_in_scope` finds no file and reports E_NO_FILE. Both are
answers a story with real work would never get, and neither raises. The
tests below are mostly about the local path NOT being taken.

LOCAL EXECUTION MODE IS UNCHANGED, and several of these say so
directly: `workspace_host` empty means "the tree is here", which is what
Docker and E0 always pass, so every read and commit takes the path it
always took.
"""
import base64

import pytest

from kernel.envelope import ExecutionEnvelope
from kernel.harness.emit import read_workspace_file
from kernel.workspace import podfs, podfs_client


class _LocalDriver:
    pass


class _PodDriver:
    WORKSPACE_MODEL = "pod"


# ── which substrate is which ─────────────────────────────────────────

def test_a_local_driver_is_not_a_pod_one():
    assert podfs_client.is_pod(_LocalDriver()) is False
    assert podfs_client.is_pod(_PodDriver()) is True


def test_an_empty_host_means_the_tree_is_here(tmp_path):
    # The Docker and E0 shape. Not "unknown" -- "local".
    (tmp_path / "report.md").write_text("findings", encoding="utf-8")
    env = ExecutionEnvelope(workspace_path=str(tmp_path))
    data, problem = read_workspace_file(env, "report.md")
    assert problem == ""
    assert data == b"findings"


def test_the_scope_check_still_refuses_an_escape_locally(tmp_path):
    env = ExecutionEnvelope(workspace_path=str(tmp_path))
    _, problem = read_workspace_file(env, "../../etc/passwd")
    assert "E_ENVELOPE_VIOLATION" in problem


# ── a pod's tree is read over the wire ───────────────────────────────

def test_a_pod_workspace_is_read_from_the_pod(monkeypatch):
    seen = {}

    def _fake(host, root, path, *, execution_id):
        seen.update(host=host, root=root, path=path,
                    execution_id=execution_id)
        return b"from the pod", ""

    monkeypatch.setattr("kernel.workspace.podfs_client.read_file", _fake)
    # BOTH HALVES ON THE ENVELOPE. podfs authenticates per execution, so
    # an envelope carrying the address and not the id names a pod it
    # cannot open -- and the 401 would arrive looking like a dead pod.
    env = ExecutionEnvelope(workspace_path="/workspace",
                            workspace_host="10.4.1.7", execution_id=31)
    data, problem = read_workspace_file(env, "report.md")

    assert (data, problem) == (b"from the pod", "")
    assert seen == {"host": "10.4.1.7", "root": "/workspace",
                    "path": "report.md", "execution_id": 31}


def test_an_envelope_with_a_host_and_no_execution_says_so(monkeypatch):
    """The half-filled envelope, refused by name.

    It cannot derive a token, so every call would 401 -- and a 401 is
    reported as unreachable, which sends the reader to the network
    instead of to whoever built the envelope.
    """
    def _never(*a, **k):
        raise AssertionError("must not reach the pod without an id")

    monkeypatch.setattr("kernel.workspace.podfs_client.read_file", _never)
    env = ExecutionEnvelope(workspace_path="/workspace",
                            workspace_host="10.4.1.7")
    data, problem = read_workspace_file(env, "report.md")
    assert data == b"" and "no execution" in problem


def test_an_unreachable_pod_refuses_rather_than_reading_locally(monkeypatch):
    # THE DEFECT THIS EXISTS FOR. Falling back would open a path in the
    # conductor's own container, find nothing, and hand back E_NO_FILE --
    # which a model reads as "I wrote it to the wrong place" and retries
    # against a workspace this process was never going to see.
    def _boom(host, root, path):
        raise podfs_client.WorkspaceUnreachable("pod is gone")

    monkeypatch.setattr("kernel.workspace.podfs_client.read_file", _boom)
    env = ExecutionEnvelope(workspace_path="/workspace",
                            workspace_host="10.4.1.7")
    data, problem = read_workspace_file(env, "report.md")
    assert data == b""
    assert "E_WORKSPACE_UNREACHABLE" in problem


def test_no_host_means_no_client_call_at_all(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("a local workspace went over the wire")

    monkeypatch.setattr("kernel.workspace.podfs_client.read_file", _boom)
    env = ExecutionEnvelope(workspace_path="/nonexistent")
    _, problem = read_workspace_file(env, "x.md")
    assert "E_NO_FILE" in problem      # local refusal, not a client one


# ── the service serves the same functions ────────────────────────────

def test_the_service_reads_through_the_same_scope_check(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    body = podfs.handle({"op": "read", "root": str(tmp_path),
                         "path": "a.txt"})
    assert base64.b64decode(body["bytes"]) == b"hello"


def test_the_service_refuses_an_escape_too(tmp_path):
    body = podfs.handle({"op": "read", "root": str(tmp_path),
                         "path": "../../etc/passwd"})
    assert "E_ENVELOPE_VIOLATION" in body["error"]


def test_bytes_cross_as_base64_because_an_artifact_is_not_text(tmp_path):
    # A report is usually text and a figure never is; guessing per
    # payload is how a PNG arrives corrupted and nobody knows which
    # layer did it.
    blob = bytes(range(256))
    (tmp_path / "fig.png").write_bytes(blob)
    body = podfs.handle({"op": "read", "root": str(tmp_path),
                         "path": "fig.png"})
    assert base64.b64decode(body["bytes"]) == blob


def test_an_unknown_operation_names_what_it_accepts():
    body = podfs.handle({"op": "rm -rf"})
    assert "E_UNKNOWN_OP" in body["error"]
    assert "commit" in body["error"] and "archive" in body["error"]


def test_a_failing_operation_never_returns_a_traceback(tmp_path):
    # A stack trace tells a model nothing it can act on and leaks paths
    # from a container it cannot see.
    body = podfs.handle({"op": "archive", "repo": str(tmp_path),
                         "commit": "does-not-exist"})
    assert "E_WORKSPACE_OP_FAILED" in body["error"]
    assert "Traceback" not in body["error"]


# ── the client refuses an address it does not have ───────────────────

def test_the_client_refuses_without_a_host():
    with pytest.raises(podfs_client.WorkspaceUnreachable) as e:
        podfs_client.call("", "read", root="/workspace", path="x",
                          execution_id=31)
    assert "empty workspace" in str(e.value)


# ── dispatch builds the shape the substrate wants ────────────────────
#
# THE ERRAND THAT FOUND THIS sat at ADMITTED for ten minutes with no
# stall reason. `dispatch_pass` called `build_sandbox_spec`
# unconditionally, which asks the driver for `sandbox_attachments` --
# host paths, a volume, a network namespace to join -- and Kubernetes
# refuses that by name because a pod's containers are declared together.
# The refusal worked; the caller was the defect.

@pytest.mark.asyncio
async def test_a_pod_driver_never_asks_for_sandbox_attachments():
    from kernel.conductor.sandbox_spec import spec_for_dispatch

    class _Refusing:
        WORKSPACE_MODEL = "pod"

        def sandbox_attachments(self, *a, **k):
            raise AssertionError("the Docker process model was assumed")

    class _Conn:
        async def fetchrow(self, sql, *a):
            if "FROM executions" in sql:
                return {"id": 1, "kind": "errand", "domain": "ops",
                        "goal_id": "G", "contract_id": None}
            return None

        async def fetch(self, sql, *a):
            return []

        async def fetchval(self, sql, *a):
            return None

    spec = await spec_for_dispatch(_Conn(), _Refusing(), 1, bridge_port=9100)
    assert spec.execution_id == 1


@pytest.mark.asyncio
async def test_a_local_driver_still_takes_the_sandbox_path(monkeypatch):
    # Docker is unchanged: it attaches a sandbox to a running worker,
    # which is exactly what build_sandbox_spec is for.
    from kernel.conductor import sandbox_spec

    called = {}

    def _fake(driver, eid, **kw):
        called["yes"] = True
        return "sandbox-spec"

    monkeypatch.setattr(sandbox_spec, "build_sandbox_spec", _fake)

    class _Local:
        pass

    out = await sandbox_spec.spec_for_dispatch(None, _Local(), 1,
                                               bridge_port=9100)
    assert out == "sandbox-spec" and called
