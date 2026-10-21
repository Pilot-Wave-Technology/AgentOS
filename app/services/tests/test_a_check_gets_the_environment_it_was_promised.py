"""The defects run 262 found, each held by the evidence that found it.

THE SHAPE THEY SHARE, and the reason they are one file: a layer made a
silent choice and the model could not see it. Every one looked like
something else first -- a broken repository, a slow suite, a platform
that does not work -- and the diagnosis cost more than the fix in all
five cases. So these tests guard the REPORTING as much as the
behaviour.
"""
from __future__ import annotations

import inspect
import json
import pathlib
import sys

import pytest

_BRIDGE = str(pathlib.Path(__file__).resolve().parents[3]
                / "kernel" / "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)
treefp = pytest.importorskip("treefp")
from kernel.harness import executor
from kernel.harness.tools_worker import checks
from kernel.worker import adopt, envcheck
from kernel.worker.contracts import normalise
from kernel.worker.leg_env import from_setups, with_mount_path


# ── 1. run_check went around the PATH wrap everything else uses ───────

def test_run_check_applies_the_mount_path():
    """It called `call_bridge` directly, so it missed the wrap
    `_call_inner` puts on every other shell path. Of 15 model-lane jobs
    on run 262, the 6 without an `export PATH` prefix were exactly the
    run_check ones -- and the criterion is REQUIRED to use the bare tool
    name, so it was the one command that most needed the PATH."""
    src = inspect.getsource(checks.run_check)
    assert "with_mount_path(" in src, (
        "run_check no longer applies the mount PATH; bare `pytest` will "
        "resolve to the image's interpreter again")
    assert "_mounts_env" in src


def test_the_wrap_is_the_same_function_the_model_s_bash_gets():
    """NOT A SECOND IMPLEMENTATION. The first attempt at this fix added
    one, which is how the two writers in `contracts` drifted apart."""
    assert "with_mount_path" in inspect.getsource(executor)
    mounts_env = from_setups([{"at": "repos/x", "bins": (".venv/bin",)}])
    wrapped = with_mount_path("cd repos/x && pytest", mounts_env)
    assert wrapped.startswith("export PATH="), wrapped
    assert "repos/x/.venv/bin" in wrapped


def test_the_note_says_which_way_it_went_either_way():
    """The empty case is the one worth saying: it looked identical to
    working for two runs."""
    silent = checks._path_note("cd x && pytest", "cd x && pytest")
    assert "image's PATH" in silent
    loud = checks._path_note('export PATH="/w/.venv/bin:$PATH"; pytest',
                             "pytest")
    assert "/w/.venv/bin" in loud


# ── 2. plan approval threw the spec away ──────────────────────────────

def test_approval_does_not_rebuild_the_checks():
    """`adopt` read `run` and `label` off each check and dropped the
    rest BEFORE `new_version` -- so `spec` went, and with it `timeout_s`
    and `kind`, for every plan ever approved. Run 262 declared 2400 and
    its suite was killed at the 1200 default at 42%."""
    src = inspect.getsource(adopt)
    assert '{"run": c.get("run", "")' not in src, (
        "the two-field rebuild is back; timeout_s and kind will be "
        "dropped at approval again")
    assert "new_version(conn, eid, checks" in src


def test_normalise_keeps_a_declared_budget_in_both_spellings():
    for check in ({"run": "pytest", "label": "goal", "timeout_s": 2400},
                  {"run": "pytest", "label": "goal",
                   "spec": {"timeout_s": 2400}}):
        assert normalise([check])[0]["spec"]["timeout_s"] == 2400, check


def test_an_artifact_check_survives_approval_as_an_artifact():
    """Dropping `kind` stored it as a shell line that can never pass."""
    got = normalise([{"run": "artifact[role=report]", "label": "goal"}])
    assert got[0]["kind"] == "artifact", got


# ── 3. the console was the second writer, with the same hole ──────────

def test_the_console_writes_criteria_through_the_shared_inserter():
    from kernel.console import contract

    src = inspect.getsource(contract)
    assert "_insert_criteria" in src, (
        "the console reimplements the criteria insert again")
    assert json.dumps({"run": "x"}) not in src, (
        "the console is back to writing only `run`, so an artifact "
        "check becomes a command that can never pass")


# ── 5. a test run moved its own fingerprint ───────────────────────────

def test_the_caches_a_test_run_writes_are_out_of_the_fingerprint():
    """Two run_check jobs, same command, no source edit, two different
    fingerprints -- so A7 never found a prior run and the reuse path was
    unreachable for any Python repo."""
    for name in ("__pycache__/", ".pytest_cache/", ".ruff_cache/"):
        assert name in treefp._CACHE_PATTERNS, name


def test_dependency_trees_are_still_inside_the_fingerprint():
    """DELIBERATELY. The registry already excludes .venv via
    .git/info/exclude where a setup ran; where it has not, a pip install
    between two runs is a real difference. Telling A7 that two trees with
    different dependencies are the same tree is the one mistake this
    mechanism must not make."""
    for name in (".venv/", "node_modules/", "target/"):
        assert name not in treefp._CACHE_PATTERNS, name


# ── 7. a setup that exits 0 and builds nothing ────────────────────────

def test_a_setup_that_built_nothing_says_so_loudly():
    found, missing = envcheck.read_probe("no repos/x/.venv/bin")
    assert (found, missing) == ([], ["repos/x/.venv/bin"])
    said = envcheck.line(found, missing)
    assert "EXITED 0" in said and "repos/x/.venv/bin" in said


def test_a_setup_that_worked_says_nothing():
    """A report that fires on every run trains the reader to skip it."""
    found, missing = envcheck.read_probe("yes repos/x/.venv/bin")
    assert envcheck.line(found, missing) == ""


def test_the_probe_asks_about_every_declared_bin():
    command = envcheck.probe_command(
        [{"at": "repos/x", "bins": (".venv/bin", "node_modules/.bin")}])
    assert "repos/x/.venv/bin" in command
    assert "repos/x/node_modules/.bin" in command


# ── 3b. the THIRD writer, missed when the first two were fixed ────────

def test_editing_a_check_on_the_card_keeps_its_budget():
    """Three writers rebuilt a check from {run, label} and dropped the
    rest. Two were fixed and this one was missed -- which is the whole
    argument for `normalise` being the only thing that builds a check.

    A person sharpening `pytest -m slow` into `pytest -m slow -x` did
    not ask for a 2400s budget to become the 1200s default, and a suite
    killed at twenty minutes reads as a failing test rather than as an
    edit that changed the clock."""
    from app.services import kernel_decide

    src = inspect.getsource(kernel_decide._rewrite_checks)
    assert '"timeout_s"' in src, (
        "an edited check loses its declared budget again")
    assert '"expect"' in src, (
        "an edited ARTIFACT check becomes a command that cannot pass")
