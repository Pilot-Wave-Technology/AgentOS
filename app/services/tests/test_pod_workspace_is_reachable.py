"""The pod's tree is findable, described, and set up.

FOUR DEFECTS, ONE SHAPE: work that used to happen in the conductor,
where the workspace was a local directory, still happens there on
Kubernetes, where it is a ReadWriteOncePod claim in another pod.

THE ONE THAT FAILED A RUN. The sandbox was given `"env": []` -- "not
'no secrets', none at all" -- and `kernel/bridge/tools_core.py` resolves
the workspace root from WORKSPACE, falling back to `/tmp`. So the shim
rooted every relative path, and `bash`'s cwd, in an empty directory.
Execution 10 called `list_dir` twelve times against a pod whose
`/workspace/repos/...` held a correct checkout the entire time, found
nothing, and failed. Proven on a live pod:

    sandbox: ls /workspace/repos/...   Dockerfile README.md app ...
    sandbox: WORKSPACE=[UNSET]
    sandbox: shim workspace root = /tmp

THE OTHER THREE ARE SILENT, which is worse. No WORKSPACE.md, so an
executor with two checkouts of one repository guesses which it may
edit. No setup discovery, so nothing is installed and the test command
fails for reasons unrelated to the work. And a setup marker written to
the conductor's disk, so `exists` is always false and the setups re-run
EVERY TICK -- a pip install per tick for the life of the run.
"""
from __future__ import annotations

import inspect

import pytest

from kernel.drivers.k8s_spec import WORKSPACE_PATH, pod_manifest
from kernel.spec import ExecutionSpec
from kernel.workspace.mounts import Mount

async def _noop_suggest(*a, **k):
    return None


MOUNT = Mount(at="repos/acme__thing", binding="git",
              source="https://github.com/acme/thing", ref="main",
              writable=True, kind="branch")


def _pod() -> dict:
    spec = ExecutionSpec(image="img", story_id="s1", execution_id=1,
                         workspace_mounts=(MOUNT,))
    return pod_manifest(spec, "exec-1", namespace="ns", pvc_name="ws-exec-1",
                        service_account="sa", bridge_image="img")


def _container(pod, name):
    for key in ("initContainers", "containers"):
        for c in pod["spec"].get(key) or []:
            if c["name"] == name:
                return c
    raise AssertionError(f"no {name} container")


# ── the sandbox can find its own tree ────────────────────────────────

def test_the_sandbox_is_told_where_the_workspace_is():
    env = {e["name"]: e.get("value")
           for e in _container(_pod(), "sandbox")["env"]}
    assert env.get("WORKSPACE") == WORKSPACE_PATH, (
        "the shim falls back to /tmp when WORKSPACE is unset, so every "
        "relative path and bash's cwd land in an empty directory while "
        "the checkout sits at /workspace")


def test_the_fallback_that_made_it_silent_is_still_there():
    """Named so the test explains itself if the fallback ever changes.

    `/tmp` is a real directory, so nothing errors -- the sandbox just
    looks at the wrong one. A fallback that raised would have been
    found in a minute.
    """
    # READ, NOT IMPORTED. `tools_core` imports `errors` flat, because
    # the shim runs it standalone with /opt/agentos-bridge on sys.path.
    # Importing it here raises ModuleNotFoundError about the test rather
    # than telling us anything about the code.
    import pathlib
    source = (pathlib.Path(__file__).resolve().parents[3] / "kernel" /
              "bridge" / "tools_core.py").read_text(encoding="utf-8")
    assert '"/tmp"' in source, (
        "the /tmp fallback changed. If it now raises instead, this whole "
        "class of silence is gone and this test should say so.")


def test_the_sandbox_still_holds_no_credential():
    """The boundary the empty list was protecting, kept explicitly.

    A mount path is not a credential; a token is. This is the assertion
    that has to survive, not `env == []`.

    TWO PATHS NOW. `HOME=/tmp` joined `WORKSPACE` on the same argument:
    `runAsUser: 65532` is an id the image has no passwd entry for, so
    HOME resolved to `/`, which that uid cannot write, and
    `env_install(pip, ...)` -- the one install path the Verifier
    reproduces -- died on `Permission denied: '/.local'`. `/tmp` is a
    directory, not a capability.

    The membership check is deliberately exact rather than a suffix
    scan: a credential does not have to be called one. Adding to this
    set is the reviewed act, and there are two of these guards in two
    files on purpose.
    """
    # AGENTOS_CREDENTIALS joined them on the same argument: it is the
    # DIRECTORY the lease was written into, not the lease. The secret is
    # the file's content, which arrives on the volume and never in this
    # list. It was set on the Docker path and missing here, so a goal
    # with a cloud grant ran on Kubernetes with the pointer absent and
    # the model could not find the credential it had been issued.
    allowed = {"WORKSPACE", "HOME", "GIT_CONFIG_COUNT",
               "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
               "AGENTOS_CREDENTIALS"}
    env = {e["name"] for e in _container(_pod(), "sandbox")["env"]}
    assert env == allowed, f"the sandbox gained {env - allowed}"
    # The one this guard is actually for now: the WORKER holds a
    # workspace token and the sandbox shares its network namespace, so
    # podfs on 127.0.0.1:9101 is reachable from the model's own bash.
    # Caught twice over -- by the exact set above and by the suffix scan
    # below -- because a secret kept out of one list and not the other is
    # not kept out.
    from kernel.workspace.podfs_auth import ENV_VAR as PODFS_TOKEN

    assert PODFS_TOKEN not in env
    for name in env:
        assert not name.endswith(("_TOKEN", "_KEY", "_SECRET", "_PASSWORD"))
    values = {e["name"]: e.get("value", "")
              for e in _container(_pod(), "sandbox")["env"]}
    assert values["HOME"] == "/tmp"


def test_the_sandbox_has_no_secret_references_either():
    # `env` is only half of it: a valueFrom/secretKeyRef carries a value
    # without naming one, which is how a credential arrives while a test
    # that reads `value` still passes.
    for entry in _container(_pod(), "sandbox")["env"]:
        assert "valueFrom" not in entry, entry


# ── the tree explains itself, from inside the pod ────────────────────

def test_the_briefing_is_written_where_the_tree_is(tmp_path):
    from kernel.workspace import podinit

    written = {}

    class _Composite:
        def acquire(self, spec):
            return spec.root

    podinit.object_fetch = None  # not used; compose imports its own
    import kernel.workspace.composite as composite
    import kernel.workspace.object_fetch as object_fetch
    old_driver, old_register = composite.CompositeDriver, object_fetch.register
    composite.CompositeDriver, object_fetch.register = _Composite, lambda: None
    try:
        podinit.compose(str(tmp_path), "[]", "1", "s1")
    finally:
        composite.CompositeDriver = old_driver
        object_fetch.register = old_register
    assert (tmp_path / "WORKSPACE.md").exists(), (
        "no execution on a pod has ever had a WORKSPACE.md: the "
        "conductor writes one after cloning, and on Kubernetes it "
        "returns before that because the pod composes its own tree")
    assert written == {}


def test_the_briefing_builder_is_shared_not_copied():
    # Two builders would drift the first time a binding was added, and
    # the pod's copy is the one nobody reads.
    from kernel.worker import runner
    from kernel.workspace import briefing
    assert runner.write_workspace_md is briefing.write_workspace_md


# ── setup discovery asks the machine the tree is on ──────────────────

def test_discovery_has_a_pod_reader():
    from kernel.verifier import setups
    assert hasattr(setups, "from_pod")
    # Same discovery rules, different reader -- not a second _discover.
    assert "_discover" in inspect.getsource(setups.from_pod)


@pytest.mark.asyncio
async def test_a_remote_workspace_is_not_read_off_the_conductor(monkeypatch):
    from kernel.worker import envsetup

    asked = {}
    def _local(*a):
        asked["local"] = True
        return []

    def _pod_reader(*a, execution_id=None):
        asked["pod"] = True
        # WHICH execution, because podfs authenticates per execution and
        # the address alone gets a 401 -- which surfaces as "unreachable"
        # and sends the reader to look at the network.
        asked["execution_id"] = execution_id
        return []

    monkeypatch.setattr(envsetup, "from_dir", _local)
    monkeypatch.setattr(envsetup, "from_pod", _pod_reader)

    class _Conn:
        async def fetch(self, *a):
            return [{"at": "repos/acme__thing", "binding": "git"}]

    await envsetup.run_setups(_Conn(), 1, "/workspace", 9100, "10.1.2.3")
    assert asked == {"pod": True, "execution_id": 1}, (
        "read off the conductor, where /workspace does not exist, so "
        "every manifest comes back missing and nothing is installed")


@pytest.mark.asyncio
async def test_a_local_workspace_still_reads_locally(monkeypatch):
    from kernel.bridge.client import BRIDGE_HOST
    from kernel.worker import envsetup

    asked = {}
    def _local(*a):
        asked["local"] = True
        return []

    def _pod_reader(*a, execution_id=None):
        asked["pod"] = True
        # WHICH execution, because podfs authenticates per execution and
        # the address alone gets a 401 -- which surfaces as "unreachable"
        # and sends the reader to look at the network.
        asked["execution_id"] = execution_id
        return []

    monkeypatch.setattr(envsetup, "from_dir", _local)
    monkeypatch.setattr(envsetup, "from_pod", _pod_reader)
    monkeypatch.setattr("kernel.worker.scout.suggest_setup",
                        _noop_suggest, raising=False)

    class _Conn:
        async def fetch(self, *a):
            return [{"at": "", "binding": "git"}]
    await envsetup.run_setups(_Conn(), 1, "/tmp/ws", 9100, BRIDGE_HOST)
    assert asked == {"local": True}


# ── and the marker they set does not lie ─────────────────────────────

@pytest.mark.asyncio
async def test_the_done_marker_is_checked_where_it_was_written(monkeypatch):
    """Otherwise the setups run again on every single tick."""
    from kernel.worker import envsetup

    seen = {}

    class _Client:
        @staticmethod
        def read_file(host, root, path, *, execution_id):
            seen["host"] = host
            seen["execution_id"] = execution_id
            return b"", ""                       # found: no problem

    monkeypatch.setattr("kernel.workspace.podfs_client.read_file",
                        _Client.read_file)
    assert await envsetup._already_done("/workspace", "exec-1", "10.1.2.3", 7)
    assert seen["host"] == "10.1.2.3"
    # AND WHICH EXECUTION, because podfs authenticates per execution now:
    # the address alone gets a 401, which surfaces as "unreachable" and
    # would send the reader to look at the network.
    assert seen["execution_id"] == 7


@pytest.mark.asyncio
async def test_an_unreachable_worker_does_not_claim_the_setups_ran():
    from kernel.worker import envsetup
    # A hiccup must not be read as "done": that leaves a run with no
    # environment and nothing saying why.
    assert not await envsetup._already_done("/workspace", "exec-1",
                                            "203.0.113.1", 7)
