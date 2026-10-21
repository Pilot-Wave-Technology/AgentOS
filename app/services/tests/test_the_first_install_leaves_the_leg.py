"""P3. `env_setup`'s first run is an init container, and it must not be
able to kill the pod.

Plan 12.5: `LEG_MAX_S` is 2700 seconds and a thirty-minute `env_setup`
inside a leg leaves fifteen minutes for the work. So the first install
moves out of the leg. Everything here is a way that move could go wrong
in a way nobody notices until a story is stuck.

THE SHELL IS GENERATED, SO IT IS PARSED. `setups.REGISTRY` is a Python
table and the container runs on the CONTRACT's image, which carries no
`kernel/` -- so the decision table travels as `sh`. A generated program
nobody parses is build 976ba352 again, where a printf escape differed
between two shells and the test passed for the wrong reason.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from kernel.drivers import k8s_spec, k8s_workspace
from kernel.spec import ExecutionSpec
from kernel.workspace import setup_script
from kernel.workspace.mounts import Mount

MOUNTS = [Mount(at="", binding="work", source="r"),
          Mount(at="repos/b", binding="needs", source="q")]


def _spec(mounts=None):
    return ExecutionSpec(image="contract:1", command=["sleep"],
                         execution_id=7,
                         workspace_mounts=MOUNTS if mounts is None else mounts)


def _pod(monkeypatch, spec):
    monkeypatch.setenv("AGENT_TOKEN_SIGNING_KEY", "test-key")
    return k8s_spec.pod_manifest(spec, "exec-7", namespace="ns",
                                 bridge_image="bridge:1", pvc_name="ws-7",
                                 service_account="agentos-execution")


def _setup(monkeypatch, spec=None):
    pod = _pod(monkeypatch, spec or _spec())
    for container in pod["spec"]["initContainers"]:
        if container["name"] == "workspace-setup":
            return container
    return None


# ── the two things that make it the RIGHT container ────────────────────

def test_it_runs_the_contracts_image_and_not_the_bridges():
    """Defence 1, which `prepare_job.py` states first. The environment
    must be built in the image the work is done in and checked against:
    a `.venv` on a different interpreter is one the sandbox cannot use,
    and a PATH shim left by our own image makes F4 meaningless."""
    spec = _spec()
    container = k8s_workspace.setup_container(
        spec, {"name": "workspace", "mountPath": "/workspace"}, 65532)

    assert container["image"] == spec.image == "contract:1"
    assert "bridge" not in container["image"]


def test_it_runs_as_the_sandbox_uid_and_not_the_workers(monkeypatch):
    """THE DIFFERENCE FROM THE CLONE CONTAINER IS DELIBERATE. The clone
    runs as the WORKER's uid because the worker commits the tree. What
    this writes -- `.venv`, `node_modules` -- is EXECUTED by the sandbox,
    and the two-uid boundary means the worker's files are not the
    sandbox's to run."""
    container = _setup(monkeypatch)

    assert container["securityContext"]["runAsUser"] == k8s_spec.SANDBOX_UID
    assert container["securityContext"]["runAsUser"] != 65531


def test_it_runs_after_the_clone(monkeypatch):
    """Init containers run in order. Before the clone it would install
    nothing and report nothing wrong -- a story whose environment
    silently never existed."""
    pod = _pod(monkeypatch, _spec())
    names = [c["name"] for c in pod["spec"]["initContainers"]]

    assert names.index("workspace-setup") > names.index("workspace-compose")


# ── and the thing that stops it being a new way to wedge a story ───────

def test_a_broken_manifest_does_not_kill_the_pod(monkeypatch):
    """AN INIT CONTAINER THAT EXITS NON-ZERO IS A POD THAT NEVER STARTS.

    Today a broken manifest is reported to the model, which fixes it in
    one leg. As a failing init container it would be a story that can
    never begin and can never be repaired -- a pod in `Init:Error` with
    no verdict and no feedback. Section 5.8 row 5 is explicit that a
    genuinely broken manifest IS about the work and the executor should
    hear it.
    """
    body = _setup(monkeypatch)["command"][2]

    assert body.rstrip().endswith("exit 0")
    assert "set -e" not in body, (
        "`set -e` would abandon the remaining mounts on the first "
        "failure, so a story with two repos loses both to one typo")


def test_every_install_command_can_fail_without_stopping_the_next():
    """One mount's broken manifest must not stop the next mount
    installing. The failure is recorded and the walk continues."""
    body = setup_script.script(["", "repos/b"], "/workspace")

    assert body.count("if [ -d ") == 2
    assert f"{setup_script.MARKER} failed" in body
    assert f"{setup_script.MARKER} ok" in body


def test_an_errand_with_no_mounts_gets_no_container(monkeypatch):
    """A container that walks no mounts is one that can still fail while
    doing nothing -- the same reason `needed` exists for the clone."""
    assert _setup(monkeypatch, _spec(mounts=[])) is None


# ── supersession, which an if/elif chain has to get right ──────────────

def test_a_lockfile_is_tested_before_the_manifest_it_supersedes():
    """`setups.from_tar` DROPS a superseded manifest when its superseder
    is present. An `elif` chain reaches the same answer only if the
    superseding one is tested first -- otherwise a repo with both
    `pnpm-lock.yaml` and `package-lock.json` installs with the wrong
    package manager, which for a pnpm workspace simply breaks."""
    body = setup_script.script([""], "/workspace")

    assert body.index("pnpm-lock.yaml") < body.index("package-lock.json")


# ── the generated program is real shell ────────────────────────────────

@pytest.mark.skipif(not shutil.which("sh"), reason="no POSIX shell")
def test_the_generated_program_parses():
    """BUILD 976ba352 IS WHY. A generated shell program nobody parses is
    one whose escaping differs between the shell here and the shell in
    the contract's image, and the first anyone knows is a story whose
    environment never built."""
    body = setup_script.script(["", "repos/b", "weird dir/x"], "/workspace")
    done = subprocess.run([shutil.which("sh"), "-n"], input=body,
                          capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr


def test_a_mount_path_with_a_space_is_quoted():
    """Directory names are repository names. `shlex.quote` or the `cd`
    silently targets the wrong place."""
    body = setup_script.script(["weird dir"], "/workspace")

    assert "'/workspace/weird dir'" in body


# ── reading the report back ────────────────────────────────────────────

def test_the_report_says_what_ran_and_what_failed():
    report = "\n".join([
        f"{setup_script.MARKER} begin",
        f"{setup_script.MARKER} start . requirements.txt",
        f"{setup_script.MARKER} ok . requirements.txt",
        f"{setup_script.MARKER} start repos/b package-lock.json",
        f"{setup_script.MARKER} failed repos/b package-lock.json",
        "    npm ERR! code ELIFECYCLE",
        f"{setup_script.MARKER} done",
    ])

    got = setup_script.outcome(report)

    assert got["ran"] == [(".", "requirements.txt")]
    assert got["failed"] == [("repos/b", "package-lock.json")]


def test_a_report_cut_off_mid_run_is_read_as_far_as_it_goes():
    """The container is best-effort by design, so a truncated report is
    not an error -- it is what there is. A parser that raised here would
    turn a partial install into no information at all."""
    got = setup_script.outcome(
        f"{setup_script.MARKER} begin\n"
        f"{setup_script.MARKER} ok . requirements.txt\n"
        f"{setup_script.MARKER} start repos/b pack")

    assert got["ran"] == [(".", "requirements.txt")]
    assert got["failed"] == []


def test_an_absent_report_is_not_an_error():
    assert setup_script.outcome("") == {"ran": [], "failed": []}
    assert setup_script.outcome(None) == {"ran": [], "failed": []}
