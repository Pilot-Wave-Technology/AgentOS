"""Taking a directory out of a pod that is about to stop existing.

THE GAP THIS CLOSES. The environment replay installs the executor's
recorded packages once, in a networked container, and carries the result
to criteria that have no network. On Docker the carrying is `GET
/containers/{id}/archive`; a Job's pod cannot be read after it exits, so
`run_ephemeral_capture` refused and `build_deps` reported ok=False — a
story whose executor installed anything could not verify on Kubernetes
at all. Safe, because every criterion came back `error` and error never
refuses, and useless.

The directory leaves under its own power now: a sidecar waits for the
work container, tars the directory into the artifact store, and prints
the id.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile

import pytest

from kernel.artifacts import capture as capture_mod
from kernel.artifacts.store import FilesystemStore, compute_id, set_store
from kernel.drivers.k8s_capture import (CAPTURE, SIGNAL_DIR, SIGNAL_FILE,
                                        WORK, artifact_id_from,
                                        capture_manifest, wrapped_command)
from kernel.spec import ExecutionSpec


@pytest.fixture
def store(tmp_path):
    binding = FilesystemStore(str(tmp_path / "artifacts"))
    set_store(binding)
    yield binding
    set_store(None)


# ── the wrapper ──────────────────────────────────────────────────────────

def test_the_original_argv_survives_being_wrapped():
    """`"$0" "$@"` re-runs the command EXACTLY. Joining and re-quoting
    would change what an approved setup script means, and `sh -c '...'`
    and a bare argv have to survive the same way."""
    assert wrapped_command(["sh", "-c", "pip install x"])[3:] == [
        "sh", "-c", "pip install x"]
    assert wrapped_command(["pytest", "-q"])[3:] == ["pytest", "-q"]
    assert wrapped_command([])[3:] == ["true"]


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("inner,expected", [
    (["sh", "-c", "exit 0"], "0"),
    (["sh", "-c", "exit 3"], "3"),
    (["sh", "-c", "echo hi; exit 7"], "7"),
])
def test_the_signal_really_is_written_by_a_real_shell(tmp_path, inner,
                                                      expected):
    """RUN, not inspected. The sidecar blocks until this file appears, so
    a wrapper that looks right and does not write it costs the whole
    grace period on every capture — and on a FAILING command, which is
    the case that matters, because `set -e` in the caller's own script
    would otherwise skip the write."""
    signal = tmp_path / "exit"
    # as_posix: a Windows path's backslashes are escapes to `sh`, and
    # the shell under test is the one that will run in a Linux pod.
    script = wrapped_command(inner)[2].replace(SIGNAL_FILE,
                                               signal.as_posix())
    result = subprocess.run(["sh", "-c", script, *inner], check=False)

    assert signal.read_text().strip() == expected
    assert result.returncode == int(expected), (
        "the wrapper must not swallow the command's own exit status")


# ── the sidecar ──────────────────────────────────────────────────────────

def test_the_tar_is_rooted_at_the_directory_basename(tmp_path):
    """THE SHAPE DOCKER'S ARCHIVE ENDPOINT RETURNS. `only_new` subtracts
    the slice's own member names and `rebase_deps_tar` re-roots them;
    both read a differently rooted tar as "nothing here" and produce an
    empty environment rather than an error."""
    src = tmp_path / "verify"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("x")

    with tarfile.open(fileobj=io.BytesIO(capture_mod.archive(str(src)))) as tf:
        assert "verify/pkg/a.py" in tf.getnames()


def test_the_capture_matches_what_the_tree_slicer_produces(tmp_path):
    """The two halves only work together: the capture is subtracted from
    a slice built by kernel/verifier/tarslice.py, so their member names
    have to agree. Asserted rather than assumed."""
    from kernel.verifier.tarslice import names

    src = tmp_path / "verify"
    src.mkdir()
    (src / "a.py").write_text("x")
    assert "verify/a.py" in names(capture_mod.archive(str(src)))


def test_a_missing_directory_captures_nothing_rather_than_raising(tmp_path):
    """A setup that failed before creating its directory is a real
    outcome; the caller reads the WORK container's exit status for the
    verdict, so this must not turn into a traceback."""
    blob = capture_mod.archive(str(tmp_path / "never-made"))
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        assert tf.getnames() == []


def test_it_waits_for_the_work_container_and_then_stores(store, tmp_path,
                                                         capsys):
    src = tmp_path / "deps"
    src.mkdir()
    (src / "p.py").write_text("y")
    signal = tmp_path / "exit"
    signal.write_text("0\n")

    assert capture_mod.capture(str(src), str(signal), timeout_s=5) == 0
    printed = capsys.readouterr().out
    artifact_id = artifact_id_from(printed)
    assert artifact_id == compute_id(store.get(artifact_id))


def test_a_work_container_that_never_signals_is_not_an_empty_capture(
        store, tmp_path, capsys):
    """AN EMPTY TAR WITH EXIT 0 IS THE DANGEROUS ANSWER. The caller would
    replay an environment with nothing in it, and criteria would fail on
    missing imports as if the work were wrong."""
    src = tmp_path / "deps"
    src.mkdir()
    assert capture_mod.capture(str(src), str(tmp_path / "nope"),
                               timeout_s=1) == 1
    assert artifact_id_from(capsys.readouterr().out) is None


def test_the_id_comes_back_through_the_log():
    """A content-addressed store means the caller cannot know the id in
    advance — it IS the hash of bytes that did not exist when the Job was
    created — so the log is the return channel."""
    assert artifact_id_from(f"noise\n{capture_mod.MARKER} {'a' * 64}\ndone") \
        == "a" * 64
    assert artifact_id_from("nothing here") is None
    # The last wins: a retried sidecar prints more than one.
    assert artifact_id_from(f"{capture_mod.MARKER} {'a' * 64}\n"
                            f"{capture_mod.MARKER} {'b' * 64}") == "b" * 64


# ── the manifest ─────────────────────────────────────────────────────────

def _manifest(capture_dir="/deps", fetch=None, **kw):
    spec = ExecutionSpec(image="py:1", command=["sh", "-c", "pip install x"],
                         execution_id=7, **kw)
    return capture_manifest(spec, "cap-1", namespace="n", timeout_s=600,
                            service_account="agentos-capture",
                            capture_dir=capture_dir,
                            capture_image="plane:sha",
                            capture_env={"ARTIFACT_STORE": "gcs"},
                            fetch=fetch, fetch_image="plane:sha",
                            fetch_env={"ARTIFACT_STORE": "gcs"})


def test_two_containers_share_the_captured_directory():
    pod = _manifest()["spec"]["template"]["spec"]
    work, sidecar = pod["containers"]
    assert (work["name"], sidecar["name"]) == (WORK, CAPTURE)
    paths = {m["mountPath"] for m in work["volumeMounts"]}
    assert paths == {m["mountPath"] for m in sidecar["volumeMounts"]}
    assert {"/deps", SIGNAL_DIR} <= paths


def test_a_capture_inside_a_fetched_directory_reuses_its_volume():
    """The prepared-mount case fetches into /tmp and captures
    /tmp/verify. A fresh emptyDir at /tmp/verify would SHADOW the tree
    that was just unpacked there, and the capture would come back empty
    — which reads downstream as a setup script that produced nothing."""
    pod = _manifest(capture_dir="/tmp/verify",
                    fetch=[("/tmp", "a" * 64)])["spec"]["template"]["spec"]
    mounts = {m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert mounts == {"/tmp", SIGNAL_DIR}, (
        "an emptyDir at /tmp/verify would hide the fetched tree")
    assert pod["initContainers"][0]["name"] == "fetch-tree"


def test_the_work_command_is_wrapped_and_the_sidecar_watches_the_signal():
    pod = _manifest()["spec"]["template"]["spec"]
    work, sidecar = pod["containers"]
    assert work["command"][:2] == ["sh", "-c"]
    assert work["command"][3:] == ["sh", "-c", "pip install x"]
    assert sidecar["command"][:3] == ["python", "-m",
                                      "kernel.artifacts.capture"]
    assert "--signal" in sidecar["command"]
    assert SIGNAL_FILE in sidecar["command"]


def test_the_sidecar_outlives_the_deadline_it_is_waiting_on():
    """It must not give up while the work container is still inside its
    own budget, or every long install captures nothing."""
    sidecar = _manifest()["spec"]["template"]["spec"]["containers"][1]
    waited = float(sidecar["command"][sidecar["command"].index("--timeout")
                                      + 1])
    assert waited > 600


def test_the_sidecar_reads_as_root_and_can_still_do_nothing_else():
    """The work container runs as whatever its image declares — root for
    a setup script that installs system packages — so a non-root sidecar
    would tar an unreadable directory and report an empty environment.
    It tars what it finds; it executes nothing."""
    sidecar = _manifest()["spec"]["template"]["spec"]["containers"][1]
    ctx = sidecar["securityContext"]
    assert ctx["runAsUser"] == 0
    assert ctx["allowPrivilegeEscalation"] is False
    assert ctx["capabilities"]["drop"] == ["ALL"]


def test_the_capture_job_never_retries_itself():
    assert _manifest()["spec"]["backoffLimit"] == 0
    assert _manifest()["spec"]["activeDeadlineSeconds"] == 600


def test_the_substrate_no_longer_says_it_cannot_capture():
    """`build_deps` refuses outright on CAPTURE_MODEL == "none", which is
    what made a story whose executor installed packages unverifiable
    here."""
    from kernel.drivers.kubernetes import KubernetesDriver

    assert KubernetesDriver.CAPTURE_MODEL != "none"


# ── the whole path, with a fake cluster ──────────────────────────────────

class _Batch:
    def __init__(self):
        self.created = None
        self.deleted = False

    # `**_` because every call now carries `_request_timeout`;
    # a fake that rejects what production sends is not a fake of
    # this API. See kernel/drivers/k8s_call.py.
    def create_namespaced_job(self, namespace, body, **_):
        self.created = body

    def read_namespaced_job_status(self, **_):
        from types import SimpleNamespace as NS
        return NS(status=NS(succeeded=1, failed=None, conditions=[]))

    def delete_namespaced_job(self, **_):
        self.deleted = True


class _Core:
    def __init__(self, sidecar_log):
        self.sidecar_log = sidecar_log

    def list_namespaced_pod(self, **_):
        from types import SimpleNamespace as NS
        return NS(items=[NS(metadata=NS(name="cap-1-x"))])

    def read_namespaced_pod_log(self, *, container, **_):
        return self.sidecar_log if container == CAPTURE else "installing..."


@pytest.fixture
def cluster(monkeypatch):
    """A driver wired to fakes, with the SDK import stubbed out."""
    from types import SimpleNamespace as NS

    from kernel.drivers import k8s_ephemeral
    from kernel.drivers.kubernetes import KubernetesDriver

    monkeypatch.setenv("AGENTOS_EXECUTION_IMAGE", "plane:sha")
    monkeypatch.setenv("ARTIFACT_STORE", "filesystem")
    monkeypatch.delenv("AGENTOS_STORE_PROXY", raising=False)

    def _make(sidecar_log):
        batch, core = _Batch(), _Core(sidecar_log)
        monkeypatch.setattr(k8s_ephemeral, "_api",
                            lambda: (NS(BatchV1Api=lambda: batch), None))
        driver = KubernetesDriver(namespace="n")
        monkeypatch.setattr(driver, "_core_api", lambda: core)
        return driver, batch
    return _make


@pytest.mark.asyncio
async def test_the_captured_directory_comes_back_as_a_tar(store, tmp_path,
                                                          cluster):
    """END TO END: the sidecar's id goes into the log, the driver reads
    it out, and the caller gets the same bytes the Docker driver's
    `GET /archive` would have handed it."""
    blob = capture_mod.archive(str(tmp_path))
    artifact_id = store.put(blob)
    driver, batch = cluster(f"{capture_mod.MARKER} {artifact_id}\n")

    result = await driver.run_ephemeral_capture(
        ExecutionSpec(image="py:1", command=["sh", "-c", "pip install x"]),
        capture="/deps", timeout=60)

    assert result["archive"] == blob
    assert result["exit_code"] == 0
    assert result["infra_error"] is None
    assert batch.deleted, "the Job must be removed whatever happened"
    names = [c["name"] for c
             in batch.created["spec"]["template"]["spec"]["containers"]]
    assert names == [WORK, CAPTURE]


@pytest.mark.asyncio
async def test_a_sidecar_that_stored_nothing_is_an_infra_error(store,
                                                               cluster):
    """NOT an empty archive with exit 0. `build_deps` gates on ok, and
    an empty tar there means every criterion runs without the packages
    the executor installed and fails on missing imports — recorded as
    the work being wrong."""
    driver, _ = cluster("the sidecar crashed before it stored anything")

    result = await driver.run_ephemeral_capture(
        ExecutionSpec(image="py:1", command=["sh", "-c", "true"]),
        capture="/deps", timeout=60)

    assert result["archive"] == b""
    assert "stored nothing" in result["infra_error"]


@pytest.mark.asyncio
async def test_the_capture_job_runs_as_the_write_identity(store, tmp_path,
                                                          cluster):
    """Not the verifier's. A criterion reads; this writes, and read and
    write are different capabilities."""
    artifact_id = store.put(capture_mod.archive(str(tmp_path)))
    driver, batch = cluster(f"{capture_mod.MARKER} {artifact_id}")
    await driver.run_ephemeral_capture(
        ExecutionSpec(image="py:1", command=["true"]), capture="/deps",
        timeout=60)

    account = batch.created["spec"]["template"]["spec"]["serviceAccountName"]
    assert account == driver.capture_service_account
    assert account != driver.verifier_service_account
    assert account != driver.service_account


def test_the_capture_module_needs_no_kubernetes_sdk_or_cloud_import():
    import ast
    import pathlib

    source = pathlib.Path(capture_mod.__file__).read_text(encoding="utf-8")
    top = {n for node in ast.parse(source).body
           if isinstance(node, (ast.Import, ast.ImportFrom))
           for n in ([node.module] if isinstance(node, ast.ImportFrom)
                     else [a.name for a in node.names])}
    assert not any((m or "").startswith(("google", "kubernetes"))
                   for m in top), f"a cloud import at module scope: {top}"
    assert "kernel.artifacts.store" not in top
