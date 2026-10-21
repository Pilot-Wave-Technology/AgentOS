"""The image a story runs in exists where the story runs.

WHAT NCAL3 DID, and it read as a parallelism problem.

Two stories, two lanes, both dispatched. `exec-189` came up and worked.
`exec-190` sat at `1/2 ImagePullBackOff`:

    Failed to pull "agentos-kernel/fixture:dev":
    docker.io/agentos-kernel/fixture:dev: not found

Pod never ready -> dispatch times out at 120s -> `reconcile: 1 recover`
-> dispatch again. Seven pods in ninety minutes, and from the outside
the only symptom was "the stories keep re-dispatching".

`agentos-kernel/fixture:dev` was a LITERAL in two kernel modules. It is
the right image -- python3, pytest, node, npm, pnpm, git, every layer
digest-pinned, and `infra/e0/fixtures/Dockerfile` says why it is not
`python:3.12-slim`: "an image that cannot run the check is
indistinguishable from a check that passes". It was also built by hand
on one laptop on 19 August 2026 and no pipeline has ever pushed it
anywhere.

AND THE OTHER STORY PROVED THE SAME POINT FROM THE OTHER SIDE. exec-189
had no contract image, fell through to the platform default
`python:3.12-slim`, ran, and parked to ask a person why `node --test`
was not on PATH. One story asked for the image and could not have it;
the other was not given it and walked into the gap it exists to close.

A FACT ONLY THE LOCAL ENVIRONMENT DECLARES IS NOT A FACT.
"""
from __future__ import annotations

import inspect
import pathlib

import pytest
import yaml

from app.services.tests.shipped_tree import IS_DISTRIBUTION, table_has_column
from kernel import base_image

_ROOT = pathlib.Path(__file__).resolve().parents[3]


# ── one declaration, and it comes from the deployment ────────────────

def test_the_deployment_names_the_image(monkeypatch):
    monkeypatch.setenv(base_image.ENV_VAR, "reg/sandbox-base:abc123")
    assert base_image.default_base_image() == "reg/sandbox-base:abc123"


def test_e0_keeps_the_local_name():
    """On E0 the laptop's Docker cache IS the registry, so the literal
    is correct there and nowhere else."""
    assert base_image.default_base_image() == base_image.E0_FIXTURE


def test_an_empty_value_is_not_a_name(monkeypatch):
    """An unset variable rendered as '' is the classic envsubst failure;
    a pod spec with image: '' fails in a way that names nothing."""
    monkeypatch.setenv(base_image.ENV_VAR, "   ")
    assert base_image.default_base_image() == base_image.E0_FIXTURE


def test_it_is_read_per_call_not_frozen_at_import():
    """The conductor and the worker are separate processes in the cloud;
    a value frozen at import in one is a value the other cannot
    correct."""
    src = inspect.getsource(base_image.default_base_image)
    assert "os.environ" in src


def test_neither_kernel_module_spells_the_literal_any_more():
    for rel in ("kernel/worker/contracts.py", "kernel/console/contract.py"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("#"))
        assert base_image.E0_FIXTURE not in code, (
            f"{rel} still hardcodes the laptop's image name")


def test_the_no_contract_path_uses_it_too():
    """THE FOURTH DECLARATION, and the one exec-189 actually took.

    A run with NO contract -- an errand, or a story before its plan is
    approved, which is every story's FIRST dispatch -- never reaches
    `contracts.mint_first`, so fixing the default there fixed nothing
    for it. `spec_builder` had its own literal, `python:3.12-slim`: no
    node, no npm, and a frontend story that wrote its tests and could
    not run them."""
    from kernel.conductor import spec_builder

    src = inspect.getsource(spec_builder)
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert '"python:3.12-slim"' not in code, (
        "the no-contract fallback still hardcodes an image with no "
        "test runner in it")
    assert "default_base_image()" in code


def test_a_goal_choice_applies_before_a_contract_exists():
    """Otherwise a goal that named a Go image is ignored for exactly the
    leg that reads the tree and writes the plan."""
    from kernel.conductor import sandbox_spec

    src = inspect.getsource(sandbox_spec)
    assert "for_goal" in src
    assert 'get("base_image")' in src, (
        "it must defer to the contract where the contract says anything")


# `test_the_dry_run_uses_a_pullable_image` stood here until E9 (plan
# 5.6). The dry run picked an image for a contract-less run and its
# fallback was the laptop's fixture name, which the cluster cannot pull.
# There is no dry run now, so the only image chooser left is
# `sandbox_spec`, asserted above against the same rule.


def test_the_house_rule_on_defaults_is_kept():
    """`kernel/` bans `environ.get(x, default)`: a default nobody can see
    is a policy nobody reviewed."""
    src = inspect.getsource(base_image)
    assert ".get(" not in src.split('"""', 2)[-1]


# ── the third rung: a goal may name its own ──────────────────────────

@pytest.mark.asyncio
async def test_a_goal_may_name_its_own_image():
    class _C:
        async def fetchval(self, sql, *a):
            assert "sandbox_base_image" in sql and "goals" in sql
            return "reg/rust-toolchain:1.83"
    assert await base_image.for_goal(_C(), "G") == "reg/rust-toolchain:1.83"


@pytest.mark.asyncio
async def test_a_goal_that_names_none_inherits_the_deployment_default():
    class _C:
        async def fetchval(self, sql, *a):
            return None
    assert await base_image.for_goal(_C(), "G") == base_image.E0_FIXTURE


def test_a_new_contract_inherits_the_goal_and_not_a_constant():
    from kernel.worker import contracts

    src = inspect.getsource(contracts)
    assert "await for_goal(conn," in src, (
        "mint_first must read the goal's image; a constant here is how "
        "the laptop's name reached the cluster")


def test_the_column_exists_and_is_sequenced():
    if IS_DISTRIBUTION:
        assert table_has_column("goals", "sandbox_base_image"), (
            "for_goal reads goals.sandbox_base_image; without the column "
            "a goal cannot name its own image")
        return
    mig = _ROOT / "migrations" / "goal_sandbox_base_image.sql"
    assert "ADD COLUMN IF NOT EXISTS sandbox_base_image" in mig.read_text(
        encoding="utf-8")
    seq = (_ROOT / "migrations" / "SEQUENCE").read_text(encoding="utf-8")
    assert "goal_sandbox_base_image" in seq.split()


# ── and something actually builds it ─────────────────────────────────

def _cloudbuild():
    return yaml.safe_load(
        (_ROOT / "cloudbuild-next.yaml").read_text(encoding="utf-8"))


# The distribution ships one pipeline, `cloudbuild.yaml`, and its plane
# step is `execution-plane` (a script call) rather than `deploy-plane`.
_PLANE_STEP = "execution-plane" if IS_DISTRIBUTION else "deploy-plane"


def _pipeline():
    if IS_DISTRIBUTION:
        return yaml.safe_load(
            (_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8"))
    return _cloudbuild()


# The step that deploys the control panel.
_PANEL_STEP = "deploy-web" if IS_DISTRIBUTION else "deploy-control-panel"


def _panel_step():
    return [s for s in _pipeline()["steps"] if s.get("id") == _PANEL_STEP][0]


def _panel_env() -> dict:
    """The control panel's build-owned env, wherever the pipeline sets it.

    `next` sets the whole list on the deploy. The distribution BAKES it
    into the image with `--build-arg`, because terraform owns the
    service's env list and an apply drops anything a deploy added.
    """
    if IS_DISTRIBUTION:
        build = [s for s in _pipeline()["steps"] if s.get("id") == "build"][0]
        args = [a for a in build["args"] if isinstance(a, str)]
        return dict(args[i + 1].split("=", 1)
                    for i, a in enumerate(args[:-1]) if a == "--build-arg")
    env_args = [a for a in _panel_step()["args"]
                if isinstance(a, str) and a.startswith("--set-env-vars=")]
    assert env_args, "the control panel step sets no env vars at all"
    return dict(kv.split("=", 1)
                for kv in env_args[0].split("=", 1)[1].split(",")
                if "=" in kv)


def test_the_pipeline_builds_the_fixture_image():
    cb = _pipeline()
    build = [s for s in cb["steps"] if s.get("id") == "build-sandbox"]
    assert build, "nothing builds the image the kernel defaults to"
    assert "infra/e0/fixtures" in build[0]["args"], (
        "it must build the SAME recipe E0 builds, or the sandbox and a "
        "developer's laptop are two environments")


def test_it_is_pushed_before_anything_is_deployed_against_it():
    cb = _pipeline()
    plane = [s for s in cb["steps"] if s.get("id") == _PLANE_STEP][0]
    assert "push-sandbox" in (plane.get("waitFor") or []), (
        "the conductor would name an image the registry does not have "
        "yet, which is the original bug with a shorter fuse")


def test_the_conductor_is_told_by_sha_not_by_tag():
    cb = _pipeline()
    plane = [s for s in cb["steps"] if s.get("id") == _PLANE_STEP][0]
    if IS_DISTRIBUTION:
        # Exported in the step's script, then read by execution-plane.sh.
        script = "\n".join(a for a in plane["args"] if isinstance(a, str))
        line = next(l for l in script.splitlines()
                    if "export SANDBOX_BASE_IMAGE=" in l)
        env = {"SANDBOX_BASE_IMAGE": line.split("=", 1)[1].strip().strip('"')}
    else:
        env = dict(e.split("=", 1) for e in plane["env"])
    assert env["SANDBOX_BASE_IMAGE"].endswith(":$COMMIT_SHA"), (
        "two verifications of one contract must see the same image; "
        "`:latest` is a moving tag")


def test_the_manifest_passes_it_to_the_kernel():
    man = (_ROOT / "deploy" / "k8s" / "conductor.yaml").read_text(
        encoding="utf-8")
    assert "AGENTOS_SANDBOX_BASE_IMAGE" in man
    assert "${SANDBOX_BASE_IMAGE}" in man


def test_the_deploy_script_exports_it():
    """`render.py` refuses an unset name rather than rendering '' -- so
    an unexported variable fails the deploy instead of the run."""
    sh = (_ROOT / "scripts" / "infra" / "deploy_execution_plane.sh").read_text(
        encoding="utf-8")
    assert "SANDBOX_BASE_IMAGE" in sh
    assert "export" in sh.split("SANDBOX_BASE_IMAGE")[-2]


def test_the_worker_inherits_the_image_name_too():
    """MEASURED ON NCAL4 RUN 191, and the fix that missed.

    `contracts.mint_first` runs in the WORKER when a plan is approved,
    and a contract's `base_image` is what every LATER dispatch pulls.
    The conductor had AGENTOS_SANDBOX_BASE_IMAGE from its manifest; the
    worker's environment is POD_ENV_ALLOWLIST and nothing else, so
    `default_base_image()` fell back to E0's literal and the contract
    was minted naming an image the cluster cannot pull.

    The pod came up 2/2 on the right image, the story planned, the plan
    was approved -- and the next dispatch went straight back to
    ImagePullBackOff. The fix had reached the process that DISPATCHES
    and missed the process that DECIDES.
    """
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    assert base_image.ENV_VAR in POD_ENV_ALLOWLIST


def test_the_control_panel_inherits_it_too_and_that_is_the_THIRD_decider():
    """THE SAME BUG, A THIRD TIME, AND THE TEST ABOVE DID NOT CATCH IT.

    Run 191 taught that the fix must reach "the process that DECIDES",
    and the test above pins that for the POD worker. It is not the only
    decider, and on `next` it is not even the usual one.

    A plan is normally approved by a PERSON in the UI, or by an
    assistant on their behalf. That request lands in the control panel
    (Cloud Run), and `app/services/kernel_decide.py` calls
    `park.resolve_request` -> `worker.adopt.adopt_checks` ->
    `contracts.new_version` -> `base_image.for_goal` ->
    `default_base_image()` -- reading the CONTROL PANEL's environment,
    which had neither variable.

    Measured on goal GOAL1, 2026-10-07: all fifteen contracts named
    `agentos-kernel/fixture:dev`, every root one minted by a human or
    an assistant approving a plan. It stayed invisible for weeks
    because a contract's image is only read by a pod that starts AFTER
    the contract exists -- a story is dispatched before its plan is
    approved, so it finishes on a pod that never consults it. Execution
    407 was the first to have its pod REPLACED afterwards, and it came
    back `Init:ImagePullBackOff` on `workspace-setup`.

    BY SHA, for the reason `test_the_conductor_is_told_by_sha_not_by_tag`
    gives: a contract that names a moving tag is a contract two
    verifications can read differently.
    """
    step = _panel_step()
    pairs = _panel_env()

    assert base_image.ENV_VAR in pairs, (
        "the control panel mints contracts and would name E0's laptop "
        "literal in every one of them")
    assert pairs[base_image.ENV_VAR].endswith(":$COMMIT_SHA"), (
        "a contract must name one immutable image, not a moving tag")

    # It cannot name an image that is not pushed yet.
    assert "push-sandbox" in step.get("waitFor", []), (
        "the control panel can go live, and mint, before the image it "
        "names exists in the registry")


def test_every_process_that_mints_a_contract_is_covered():
    """The list, so the next decider is added here rather than found.

    Three processes call `for_goal` and write the answer into a
    contract, and each one was fixed only after it had already minted
    bad contracts in production:

      conductor      deploy/k8s/conductor.yaml        (run 190)
      pod worker     POD_ENV_ALLOWLIST                (run 191)
      control panel  cloudbuild-next.yaml             (GOAL1, 407)

    If a fourth appears, it belongs here.
    """
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST

    manifest = (_ROOT / "deploy" / "k8s" / "conductor.yaml").read_text(
        encoding="utf-8")
    assert base_image.ENV_VAR in manifest
    assert base_image.ENV_VAR in POD_ENV_ALLOWLIST
    assert base_image.ENV_VAR in _panel_env()


def test_the_sandbox_can_run_git_in_a_tree_it_does_not_own():
    """The init container clones as the worker's uid; the sandbox runs
    as 65532. Git then refuses every command in the checkout with
    `fatal: detected dubious ownership`, so `git status`, `git diff` and
    `git log` -- the first things an executor runs to orient -- all
    fail. Invisible until now only because the old sandbox image had no
    `git` at all: "not found" rather than "refused"."""
    from kernel.drivers import k8s_spec

    src = inspect.getsource(k8s_spec.pod_manifest)
    assert '"value": "safe.directory"' in src
    assert '{"name": "GIT_CONFIG_COUNT", "value": "1"}' in src


def test_the_sandbox_has_a_writable_home():
    """MEASURED IN THE IMAGE, as uid 65532.

    `runAsUser: 65532` is an id the image has no passwd entry for, so
    HOME resolves to `/` and the sandbox cannot write it. That costs the
    one install path the Verifier can reproduce: `env_install(pip, ...)`
    runs a bare `pip install`, system site-packages is root-owned, pip
    falls back to the user site and dies with

        OSError: [Errno 13] Permission denied: '/.local'

    -- a permission error naming a path nobody chose, from the tool
    whose description is "use this instead of running pip through the
    shell". `/tmp` is 1777, on the container's own layer, and is not the
    workspace, so pip's cache stays out of what `story_commit` walks.
    """
    from kernel.drivers import k8s_spec

    src = inspect.getsource(k8s_spec.pod_manifest)
    assert '{"name": "HOME", "value": "/tmp"}' in src


# ── what a sandbox can reach, which is the other half of "installable"
# ────────────────────────────────────────────────────────────────────

def _allowlist() -> str:
    man = (_ROOT / "deploy" / "k8s" / "conductor.yaml").read_text(
        encoding="utf-8")
    return "\n".join(l for l in man.splitlines() if "full_hosts" in l)


@pytest.mark.parametrize("host", [
    ".pypi.org", ".npmjs.org",          # what already worked
    ".crates.io", ".golang.org", ".maven.org", ".rubygems.org",
    ".packagist.org", ".nuget.org",     # what did not
])
def test_a_package_index_is_reachable(host):
    """`setups.py` is language-blind on purpose -- it runs the repo's own
    `.agentos/setup.sh` and never learns what pip or npm are. A repo was
    free to declare a Rust or Go environment and then could not fetch
    it: the proxy denied the host, and the failure surfaced as a setup
    command that simply did not work."""
    assert host in _allowlist()


def test_the_allowlist_is_still_a_list_and_not_an_opening():
    man = (_ROOT / "deploy" / "k8s" / "conductor.yaml").read_text(
        encoding="utf-8")
    assert "http_access deny all" in man
    assert "dstdomain .com\n" not in man and "dstdomain .org\n" not in man
