"""A capture pod that cannot name its execution gets reaped mid-install.

MEASURED ON EXECUTION 208. A capture Job was seven seconds into `pip
install` when reconcile ran, and the conductor killed both of its
containers. `capture_manifest` stamps `managed-by: kernel` — correct,
the pod IS ours, so `list_containers()` returns it — and stamps
`execution-id` ONLY when the spec carries one. Neither capture call
site set it:

    kernel/verifier/prepare.py     _prepare_mount   the setup replay
    kernel/verifier/environment.py build_deps       the env replay

So `orphan_sweep` saw a kernel-owned pod it could match to no
execution, concluded "no matching execution", and destroyed it with
`stop_timeout=0`.

WHY IT READ AS FLAKE. A capture runs about 28 seconds and reconcile
lands every 15 ticks, so it only bites on overlap. Three of four
captures survived; the fourth looked like an infrastructure hiccup, and
`error` retries papered over it until the retries ran out.

The label is not the whole defence — a pod with its own controller
should not be racing a second reaper at all, which
`test_the_sweep_does_not_reap_a_live_job.py` asserts separately. This
file asserts the other half: the id EXISTS to be matched. Two
independent guards, because the one that was missing here cost a day.

`execution_id` is a keyword with a default all the way down, because a
caller with no execution is legitimate — the corpus validator verifies
a fixture that was never dispatched.
"""
from __future__ import annotations

import ast
import io
import pathlib
import tarfile

import pytest

from kernel.drivers.k8s_capture import capture_manifest
from kernel.spec import ExecutionSpec
from kernel.verifier.environment import build_deps
from kernel.verifier.prepare import build_prepared

EID = 208


class _Driver:
    """Records the spec it was handed. Nothing reaches a cluster."""

    # `CLAIM_JOB_MODEL`, WHICH THE REAL DRIVERS DECLARE. This fake used
    # to carry `name = "kubernetes"` -- an attribute NO driver in the
    # tree defines -- and it was the only thing in the system that made
    # `getattr(driver, "name", "") != "kubernetes"` false. So these
    # tests proved the sweep listed, triaged and deleted correctly while
    # the sweep returned [] on every real tick. A fake that invents the
    # attribute a guard needs turns that guard into a test fixture.
    CLAIM_JOB_MODEL = "job"
    name = "kubernetes"

    def __init__(self):
        self.specs = []

    async def run_ephemeral_capture(self, spec, capture, timeout=300.0,
                                    **kwargs):
        self.specs.append(spec)
        return {"exit_code": 0, "output": "", "archive": _tar(),
                "infra_error": None, "timed_out": False}


def _tar(names=("verify/README.md",)) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 0
            tf.addfile(info, io.BytesIO(b""))
    return buf.getvalue()


# ── the manifest, given a spec that names its run ──────────────────────

def test_the_label_appears_when_the_spec_carries_the_id():
    manifest = capture_manifest(
        ExecutionSpec(image="py:1", command=["true"], execution_id=EID),
        "cap-abc", namespace="agentos-staging", timeout_s=60,
        service_account="sa", capture_dir="/deps", capture_image="img")
    for where in (manifest["metadata"]["labels"],
                  manifest["spec"]["template"]["metadata"]["labels"]):
        # BOTH, because the sweep reads the POD's labels and a person
        # reads the Job's, and a label on only one of them is the half
        # fix that looks done.
        assert where["execution-id"] == str(EID)


# ── and the two call sites that never set it ───────────────────────────

@pytest.mark.asyncio
async def test_the_environment_replay_stamps_the_execution_it_serves():
    driver = _Driver()
    await build_deps(driver, "py:1",
                     [{"manager": "pip", "packages": ["fastapi"],
                       "reproducible": True}],
                     timeout=60, execution_id=EID)
    assert driver.specs, "the replay never reached the capture seam"
    assert driver.specs[0].execution_id == EID


@pytest.mark.asyncio
async def test_the_setup_prepare_stamps_the_execution_it_serves(monkeypatch):
    """Cache disabled: a hit returns before the spec is ever built, and
    a test that silently exercised the cache would assert nothing."""
    monkeypatch.setattr("kernel.verifier.prepare._cache_get",
                        lambda key: None)
    monkeypatch.setattr("kernel.verifier.prepare._cache_put",
                        lambda key, blob: False)
    driver = _Driver()
    setups = [{"at": "", "source": ".agentos/setup.sh",
               "command": "pip install -e .", "content": b"pip install -e .",
               "bins": [".venv/bin"]}]
    await build_prepared(driver, "py:1", _tar(), setups,
                         timeout=60, execution_id=EID)
    assert driver.specs, "the prepare never reached the capture seam"
    assert driver.specs[0].execution_id == EID


@pytest.mark.asyncio
async def test_no_execution_is_still_a_legitimate_caller():
    """The corpus validator verifies a fixture that was never
    dispatched. It must not have to invent an id to run."""
    driver = _Driver()
    await build_deps(driver, "py:1",
                     [{"manager": "pip", "packages": ["fastapi"],
                       "reproducible": True}], timeout=60)
    assert driver.specs[0].execution_id is None


# ── and the caller in the middle actually passes it on ─────────────────

def _verify_source() -> ast.AST:
    """E2 SPLIT `verify.py`; the two capture calls went to `stage.py`.

    The assertion below is about the CALLER IN THE MIDDLE — the code
    between `verify_claim`'s parameter and the capture seam — and that
    code is now `kernel/verifier/stage.py`, unchanged but relocated.
    Reading the old file would have passed by finding no calls at all,
    which is why the "retire this case" assertion is there.
    """
    root = pathlib.Path(__file__).resolve().parents[3]
    return ast.parse((root / "kernel" / "verifier" / "stage.py")
                     .read_text(encoding="utf-8"))


@pytest.mark.parametrize("callee", ["build_deps", "build_prepared"])
def test_verify_claim_hands_the_id_down_to_both_captures(callee):
    """THE THREADING IS THE FIX, not the parameter. `verify_claim` has
    taken `execution_id` since the pod-address work; what was missing
    was the two lines that pass it on, and a default of None means
    forgetting one is silent — no error, just a pod a reaper cannot
    place. Asserted against the source because the alternative is
    standing up a whole verification."""
    calls = [n for n in ast.walk(_verify_source())
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == callee]
    assert calls, f"verify.py no longer calls {callee} — retire this case"
    for call in calls:
        passed = {kw.arg for kw in call.keywords}
        assert "execution_id" in passed, (
            f"verify.py:{call.lineno} calls {callee} without execution_id; "
            f"its capture Job will carry no `execution-id` label and the "
            f"orphan sweep will treat it as a leak")
