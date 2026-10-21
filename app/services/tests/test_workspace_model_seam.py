"""A workspace the conductor cannot reach is refused, not retried.

On Kubernetes the execution's workspace is a ReadWriteOncePod claim
belonging to the execution's OWN pod. The conductor cannot mount it --
the access mode exists so the API server refuses a second writer -- and
the pod has no init container, and the driver has no exec. Nothing
populates it yet.

prepare_workspace ran the Docker path anyway and died on

    [Errno 13] Permission denied: '/workspace'

every tick: a permissions error for something that is not a permissions
problem, retried for a capability that cannot appear. Both halves sent a
person to the wrong place.
"""
import pytest

from kernel.driver import Driver
from kernel.drivers.kubernetes import KubernetesDriver
from kernel.worker.runner import WorkspaceUnsupported, prepare_workspace
from kernel.workspace.mounts import Mount

# A REAL MOUNT, not `object()`. I3 reads the mount's own fields at
# admission -- a copyin source that is a local directory cannot be
# materialised in a pod and is refused by name there -- so a stand-in
# with no fields no longer stands for anything a caller can pass.
_REPO = Mount(at="repos/acme__web", binding="git",
              source="https://github.com/acme/web", writable=True)


def test_the_default_is_local():
    # Docker and E0 compose the workspace where the conductor runs, and
    # nothing about that changes.
    assert Driver.WORKSPACE_MODEL == "local"


def test_kubernetes_says_pod():
    assert KubernetesDriver.WORKSPACE_MODEL == "pod"


class _Conn:
    def __init__(self, row=None):
        self.row = row or {"workspace_source": None, "workspace_path": None}
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetchrow(self, sql, *a):
        return self.row

    async def fetchval(self, sql, *a):
        return None


class _PodDriver:
    WORKSPACE_MODEL = "pod"


def _git_mount():
    """A REAL mount, not a bare object().

    These tests used `object()` as a placeholder, which passed only
    while nothing looked inside it. I3 then added a check that reads
    `mount.binding` -- correctly -- and both tests failed on the fake
    rather than on the behaviour. A stand-in that cannot survive the
    code reading it is a stand-in that tests nothing.
    """
    from kernel.workspace.mounts import Mount

    return Mount(at="repos/o__r", binding="git",
                 source="https://example.invalid/o/r.git", writable=True,
                 kind="repo")


@pytest.mark.asyncio
async def test_a_pod_driver_records_the_path_and_touches_nothing_locally(
        monkeypatch):
    """SUPERSEDES two tests that asserted a refusal.

    They read `test_a_pod_driver_with_mounts_refuses_by_name` and
    `test_it_refuses_before_touching_the_filesystem`, and they were right
    while nothing populated the claim: composing locally would either
    fail as `[Errno 13] Permission denied: '/workspace'` or, worse,
    succeed against a directory in the conductor's own container that
    the pod would never see.

    I2 populates it. `kernel/drivers/k8s_workspace.py` adds an init
    container running `kernel.workspace.podinit`, which calls the SAME
    `CompositeDriver.acquire`. So the conductor now records where the
    workspace will be and does nothing else -- and "does nothing else"
    is still the load-bearing half, which is why the makedirs trap below
    survived the rewrite.
    """
    monkeypatch.setattr("kernel.worker.runner.mounts_for",
                        _fake_mounts([_git_mount()]))

    def _boom(*a, **k):
        raise AssertionError("the conductor composed a pod's workspace")

    monkeypatch.setattr("os.makedirs", _boom)

    conn = _Conn()
    path = await prepare_workspace(conn, 1, "errand", "ops", "/workspace",
                                   driver=_PodDriver())

    assert path == "/workspace"
    # Recorded, because `worker/loop.py` refuses to rebuild a sandbox
    # without a workspace_path -- "a rebuild would start from nothing,
    # which is a new run wearing an old id".
    assert any("workspace_path" in sql for sql, _ in conn.executed)


@pytest.mark.asyncio
async def test_a_local_directory_source_is_still_refused(monkeypatch):
    """The one refusal that does NOT stop being true when I2 lands.

    A `copyin` mount whose source is a path on the conductor cannot be
    materialised in a pod however good the init container is, so this
    stays a refusal and stays ahead of everything else.
    """
    from kernel.workspace.mounts import Mount

    local = Mount(at="docs/spec.md", binding="copyin",
                  source="/srv/corpus/spec.md", writable=False,
                  kind="document")
    monkeypatch.setattr("kernel.worker.runner.mounts_for",
                        _fake_mounts([local]))
    with pytest.raises(WorkspaceUnsupported) as e:
        await prepare_workspace(_Conn(), 1, "errand", "ops", "/workspace",
                                driver=_PodDriver())
    assert "/srv/corpus/spec.md" in str(e.value)


def _fake_mounts(result):
    async def _mounts_for(conn, execution_id):
        return result
    return _mounts_for
