"""I4 — the two readers that still opened a local filesystem.

The verifier's tree and the assistant panel's explorer were the last
places where the conductor assumed it could see the execution's files.
On Kubernetes it cannot: the claim is ReadWriteOncePod and belongs to
the execution's own pod, so `git -C /workspace archive` and `os.walk`
both SUCCEED and both return nothing. Neither raises, and an empty tar
reads as "the story committed nothing" while an empty walk reads as "no
files in this workspace". Those are the wrong answers that look like
facts, which is what these tests are here to stop.

TWO HALVES, AND THE FIRST ONE IS THE IMPORTANT ONE:

  local is untouched   Docker and E0 take the same code path, with the
                       same arguments, and never construct or call the
                       pod client at all.
  pod routes, or it    A pod-model driver crosses to the worker; a pod
  refuses              it cannot reach refuses, and the refusal carries
                       no tree — because an empty result is exactly the
                       failure being prevented.

Nothing here makes a network call: `podfs_client` is faked where the
crossing succeeds, and where it fails it is the REAL client, refusing
on an empty address before it opens a socket.
"""
from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace

import pytest

from kernel.driver import Driver
from kernel.drivers.docker import DockerDriver
from kernel.verifier.tree import archive_in_pod, archive_is_remote
from kernel.verifier.verify import verify_claim
from kernel.workspace import browse as browse_mod
from kernel.workspace.podfs_client import WorkspaceUnreachable

POD_IP = "10.4.1.7"
ROOT = "/workspace"
COMMIT = "c0ffee1234567890"
CRITERION = {"id": 1, "kind": "command", "spec": {"run": "true"}}


def tar_of(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as out:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class LocalDriver:
    """Whatever Docker and E0 are: no ARCHIVE_MODEL, no pod."""


class PodDriver:
    ARCHIVE_MODEL = "pod"
    WORKSPACE_MODEL = "pod"

    def __init__(self, host: str = POD_IP):
        self._host = host
        self.closed = False

    async def resolve_bridge_host(self, container_id: str) -> str:
        return self._host

    async def aclose(self) -> None:
        self.closed = True


class Conn:
    """Only what these two paths ask of a connection."""

    def __init__(self, execution_id=7, container_id="exec-7-abc",
                 recorded_host=None):
        # None = nothing recorded on the row, so these tests keep
        # exercising the registry fallback they were written for.
        self.recorded_host = recorded_host
        self.execution_id = execution_id
        self.container_id = container_id

    async def fetchval(self, sql, *args):
        # `resolve_execution`'s query names container_id too, in its
        # ORDER BY — matching on the word alone answered a story lookup
        # with a pod name and every test still passed.
        if sql.strip().startswith("SELECT container_id"):
            return self.container_id
        # The recorded address, added with the column the docstring
        # above asked for. Named explicitly for the same reason the
        # container_id branch is: this fake's catch-all returns the
        # execution id, so an unmatched query answers a HOST with the
        # number 7 and the test still passes while asserting nothing.
        if sql.strip().startswith("SELECT workspace_host"):
            return self.recorded_host
        return self.execution_id


def mounts(*ats):
    return [{"at": at, "kind": "git", "writable": i == 0,
             "root": f"{ROOT}/{at}" if at else ROOT}
            for i, at in enumerate(ats)]


def no_client(monkeypatch):
    """Every pod entry point, wired to fail the test if it is called."""
    def _boom(*a, **k):
        raise AssertionError("a local execution called the pod client")

    for name in ("archive", "explore", "call"):
        monkeypatch.setattr(f"kernel.workspace.podfs_client.{name}", _boom)


# ── the seam, and its default ────────────────────────────────────────

def test_the_archive_is_taken_here_unless_a_driver_says_otherwise():
    from kernel.drivers.kubernetes import KubernetesDriver

    assert Driver.ARCHIVE_MODEL == "local"
    assert DockerDriver.ARCHIVE_MODEL == "local"
    assert "ARCHIVE_MODEL" not in DockerDriver.__dict__
    assert archive_is_remote(DockerDriver) is False
    # E0 hands `verify_claim` things that are not drivers at all.
    assert archive_is_remote(None) is False
    assert archive_is_remote(object()) is False
    # And stated on Kubernetes SEPARATELY from WORKSPACE_MODEL, even
    # where the two agree: one is about who may write the workspace,
    # the other about whether a commit can be read out of it.
    assert KubernetesDriver.ARCHIVE_MODEL == "pod"
    assert "ARCHIVE_MODEL" in KubernetesDriver.__dict__


# ── local execution is untouched ─────────────────────────────────────

@pytest.mark.asyncio
async def test_a_local_verification_archives_here_with_the_same_arguments(
        monkeypatch):
    no_client(monkeypatch)
    seen = {}

    def _archive(repo, commit, **kwargs):
        seen.update(repo=repo, commit=commit, **kwargs)
        return tar_of({"verify/README.md": "hi"})

    monkeypatch.setattr("kernel.verifier.stage.archive_committed_tree",
                        _archive)
    out = await verify_claim(LocalDriver(), {}, [], "/srv/work", COMMIT)

    assert out == []
    assert seen["repo"] == "/srv/work" and seen["commit"] == COMMIT
    assert seen["prefix"] == "verify/" and seen["uid"] == 65534
    assert seen["inject"] == {}


@pytest.mark.asyncio
async def test_a_local_panel_read_never_reaches_for_the_client(monkeypatch):
    no_client(monkeypatch)
    monkeypatch.setattr("kernel.workspace.explorer.mounts_of",
                        _mounts_of(mounts("repos/acme__api")))
    monkeypatch.setattr("kernel.workspace.explorer.tree",
                        lambda root, at="", writable=False: {
                            "at": at, "entries": [{"path": "a.py"}]})

    got = await browse_mod.browse(Conn(), "de5512b2", driver=LocalDriver())

    assert got["entries"] == [{"path": "a.py"}]
    assert "error" not in got


def test_the_local_explorer_is_resolved_when_it_is_called(monkeypatch):
    # Not bound into a dict at import: `browse` and `explorer` must not
    # be able to drift into two definitions of the same three reads.
    monkeypatch.setattr("kernel.workspace.explorer.diff",
                        lambda root, rel="": "patched")
    assert browse_mod.local("diff", ROOT, rel="a.py") == "patched"


# ── the pod path ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_pod_driver_takes_its_archive_in_the_pod(monkeypatch):
    seen = {}

    def _archive(host, repo, commit, prefix="verify/", uid=0,
                 exclude=None, *, execution_id=None):
        seen.update(host=host, repo=repo, commit=commit, prefix=prefix,
                    uid=uid, execution_id=execution_id)
        return tar_of({"verify/README.md": "hi"})

    monkeypatch.setattr("kernel.workspace.podfs_client.archive", _archive)
    monkeypatch.setattr("kernel.verifier.stage.archive_committed_tree",
                        _never("the conductor archived a pod's workspace"))

    # BOTH, because podfs authenticates per execution. Three callers --
    # the adjudicator, the dry-run and the inert probe -- passed NEITHER,
    # so on a pod every one of them reached `archive_in_pod("")`, which
    # refused, which is `error` for every criterion of every story.
    out = await verify_claim(PodDriver(), {}, [], ROOT, COMMIT,
                             workspace_host=POD_IP, execution_id=77)

    assert out == []
    assert seen == {"host": POD_IP, "repo": ROOT, "commit": COMMIT,
                    "prefix": "verify/", "uid": 65534, "execution_id": 77}


def test_the_held_out_set_is_added_on_this_side_and_never_crosses(
        monkeypatch):
    """F4 survives the crossing, by not making it.

    `podfs` has no `inject` argument and must not grow one: the
    held-out checks are "absent from the executor entirely" and the
    execution's pod is the executor's pod. They are written into the
    copy the Verifier receives, one process later than usual.
    """
    sent = {}

    def _archive(host, repo, commit, **kwargs):  # noqa: ARG001
        sent.update(kwargs)
        return tar_of({"verify/app.py": "x", "verify/tests/test_a.py": "old"})

    monkeypatch.setattr("kernel.workspace.podfs_client.archive", _archive)

    tar = archive_in_pod(POD_IP, ROOT, COMMIT,
                         inject={"tests/test_a.py": "held out",
                                 "tests/test_b.py": "held out too"})

    assert "inject" not in sent
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r") as src:
        got = src.getnames()
    assert got.count("verify/tests/test_a.py") == 2
    # AFTER the repository's own entries, so the held-out copy is the
    # one that lands on extraction.
    assert got.index("verify/tests/test_b.py") > got.index("verify/app.py")
    assert got[-2:] == ["verify/tests/test_a.py", "verify/tests/test_b.py"]


# ── and a pod it cannot reach refuses ────────────────────────────────

@pytest.mark.asyncio
async def test_an_unreachable_pod_is_an_error_and_not_an_empty_tree(
        monkeypatch):
    # THE REAL CLIENT. It refuses on an empty address before it opens a
    # socket, which is the behaviour under test.
    monkeypatch.setattr("kernel.verifier.stage.archive_committed_tree",
                        _never("the conductor read its own filesystem"))

    out = await verify_claim(PodDriver(), {}, [CRITERION], ROOT, COMMIT,
                             workspace_host="")

    (_criterion_row, outcome), = out
    # `error`, never `fail`: nothing was checked, so nothing was found
    # wrong. A `fail` here would refuse a completion claim over an
    # address the conductor did not have.
    assert outcome.passes == "error"
    assert "no address" in outcome.detail["error"]


@pytest.mark.asyncio
async def test_a_multi_mount_pod_workspace_is_archived_whole(monkeypatch):
    """THE PROPERTY IS KEPT; THE REFUSAL IS NOT THE PROPERTY.

    This used to assert an error, because `podfs` exposed only the git
    half of a workspace and "a git-only archive would hand the criteria
    a tree with every document missing". Refusing was the honest
    response to that limit.

    The limit is gone: `archive_mounts` runs in the pod now, where the
    git checkouts AND the copied-in documents both are. Keeping the
    refusal would have been fatal rather than cautious -- on Kubernetes
    EVERY workspace is `repos/<owner>__<name>`, so `any(m.at)` is always
    true and no story could ever be verified.

    What must still never happen is the git-only crossing, or the
    conductor reading its own filesystem. Both are still refused here.
    """
    monkeypatch.setattr("kernel.workspace.podfs_client.archive",
                        _never("a git-only archive crossed to the pod"))
    monkeypatch.setattr("kernel.verifier.stage.archive_committed_tree",
                        _never("the conductor read its own filesystem"))

    seen = {}

    def _whole(host, root, mounts, prefix="verify/", uid=0,
               *, execution_id=None):
        seen.update(host=host, root=root,
                    mounts=[m.at for m in mounts], prefix=prefix)
        return tar_of({f"{prefix}repos/acme__api/ok.txt": "x"})

    monkeypatch.setattr("kernel.workspace.podfs_client.archive_mounts",
                        _whole)

    out = await verify_claim(PodDriver(), {}, [CRITERION], ROOT, COMMIT,
                             mounts=[SimpleNamespace(at="repos/acme__api"),
                                     SimpleNamespace(at="docs/RULES.md")],
                             workspace_host=POD_IP)

    assert seen["host"] == POD_IP, "it must archive IN THE POD"
    assert seen["mounts"] == ["repos/acme__api", "docs/RULES.md"], (
        "both halves must cross, or the documents are missing again")
    # NOT the old blanket refusal. (This fixture hands `verify_claim` an
    # empty contract, so it still errors further down on "the contract
    # names no base image" -- a different fact, and not this one.)
    (_row, outcome), = out
    assert "several mounts" not in str(outcome.detail)


# ── the panel, on a pod ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_panel_reads_a_pod_workspace_from_the_worker(monkeypatch):
    calls = []

    def _explore(host, what, root, *, execution_id=None, **args):
        calls.append((host, what, root, args))
        return {"at": args.get("at"), "entries": [{"path": "main.py"}]}

    monkeypatch.setattr("kernel.workspace.podfs_client.explore", _explore)
    monkeypatch.setattr("kernel.workspace.explorer.tree",
                        _never("the conductor walked a pod's workspace"))
    monkeypatch.setattr("kernel.workspace.explorer.mounts_of",
                        _mounts_of(mounts("repos/acme__api", "docs/R.md")))

    driver = PodDriver()
    got = await browse_mod.browse(Conn(), "de5512b2", want="all",
                                  driver=driver)

    assert [c[1] for c in calls] == ["tree", "tree"]
    assert {c[0] for c in calls} == {POD_IP}
    assert got["file_count"] == 2
    assert driver.closed is False        # the caller's driver, not ours


def test_the_panel_addresses_the_worker_and_not_the_sandbox(monkeypatch):
    """D5, and it is a property rather than a preference.

    The sandbox is the container the MODEL controls. A panel whose
    contents the model can shape is not a view of the truth, so the
    explorer is served by the worker — which is also where the git
    credentials already are, for archive, commit and publish.
    """
    from kernel.drivers.kubernetes import BRIDGE_PORT
    from kernel.workspace import podfs_client

    seen = {}

    def _open(request, timeout=None):
        seen["url"] = request.full_url
        return io.BytesIO(b'{"result": {"entries": []}}')

    # THE SEAM MOVED, and it is worth saying where. podfs is reached
    # through an opener carrying an EMPTY ProxyHandler rather than the
    # module-level `urlopen`, because an in-cluster address sent to the
    # egress proxy comes back 403 -- see `podfs_client._DIRECT`. The
    # property under test is unchanged: the panel dials the worker.
    monkeypatch.setattr(podfs_client._DIRECT, "open", _open)
    podfs_client.explore(POD_IP, "tree", ROOT, execution_id=77)

    assert seen["url"] == f"http://{POD_IP}:9101/"
    assert BRIDGE_PORT == 9100          # the shim. Deliberately not this.


@pytest.mark.asyncio
@pytest.mark.parametrize("conn, driver, says", [
    (Conn(container_id=None), PodDriver(), "no pod"),
    (Conn(), PodDriver(""), "no address"),
    (Conn(), PodDriver(), "connection refused"),
])
async def test_a_pod_it_cannot_reach_refuses_and_shows_no_tree(
        monkeypatch, conn, driver, says):
    """Three ways to lose the pod, one answer: say so, show nothing.

    The pod was never dispatched or has been reaped; the substrate has
    no address for it; the worker does not answer. Falling back to the
    local read in any of them walks the conductor's own container and
    reports what it finds — nothing — as this story's workspace.
    """
    monkeypatch.setattr("kernel.workspace.explorer.mounts_of",
                        _mounts_of(mounts("repos/acme__api")))
    monkeypatch.setattr("kernel.workspace.explorer.tree",
                        _never("an unreachable pod fell back to local"))
    monkeypatch.setattr("kernel.workspace.podfs_client.explore",
                        _raising("connection refused"))

    got = await browse_mod.browse(conn, "de5512b2", want="all",
                                  driver=driver)

    assert says in got["error"]
    # NOT an empty workspace. A payload carrying `entries` or `trees`
    # renders as "no files here", which is the one thing this must
    # never be mistaken for.
    assert "trees" not in got and "entries" not in got


# ── helpers ──────────────────────────────────────────────────────────

def _never(why):
    def _fail(*a, **k):
        raise AssertionError(why)
    return _fail


def _raising(why):
    def _refuse(*a, **k):
        raise WorkspaceUnreachable(why)
    return _refuse


def _mounts_of(rows):
    async def _fake(conn, execution_id):
        return rows
    return _fake


# ── the row beats the process ────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_recorded_address_wins_over_the_serving_process():
    """The fix for the panel answering "docker" on Cloud Run.

    `registry.resolve()` reads AGENTOS_DRIVER from whatever process is
    running, and only the conductor's deployment sets it. The explorer
    is served by the control panel -- a DIFFERENT deployment -- so for a
    Kubernetes execution it resolved a Docker driver and walked its own
    container, presenting an empty panel as the truth.

    The conductor now writes `executions.workspace_host` at dispatch,
    where the address was actually resolved, and this must be believed
    over anything the local process thinks.
    """
    from kernel.workspace.browse import workspace_host

    class _SaysDocker:
        pass                                   # WORKSPACE_MODEL defaults local

    host = await workspace_host(Conn(recorded_host="10.4.1.9"), 7,
                                driver=_SaysDocker())
    assert host == "10.4.1.9"


@pytest.mark.asyncio
async def test_no_recorded_address_still_means_local_for_a_local_driver():
    # The Docker and E0 shape, unchanged: nothing recorded, no pod.
    from kernel.workspace.browse import workspace_host

    class _Local:
        pass

    assert await workspace_host(Conn(), 7, driver=_Local()) == ""
