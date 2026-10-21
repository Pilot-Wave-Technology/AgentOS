"""Four gaps in how verification knows what it is looking at.

**A — the environment's identity was two named files.** The key hashed
`.agentos/setup.sh`, `.agentos/env` and each recognised manifest.
`setup.sh` is arbitrary shell: one that reads a version file beside
itself changes what it installs without changing a hashed byte, the
stamp stays current, `prepare_script` step 1 skips the install, and the
run is verified against an environment its manifest would not build.

**B — and hashing cannot close that.** The input is a program. So the
drift window is bounded by ROUNDS instead: every few generations the
environment is rebuilt from scratch whatever the key says.

**C — the assessor was given a repository and told to find the work.**
Measured here: 2,447 tracked files, `list_files` capped at 400 in
ALPHABETICAL order, showing 14 of 488 test files and nothing under
`tests/`. An assessor asked "are there tests for this?" answers no,
confidently, and refuses work that is tested. It now gets the diff
first, and every truncation says it truncated.

**D — and the one class of work with nothing to install could never be
verified in the pod.** No manifest meant no prepare Job, so no tree on
the volume, no `env_key`, and `warm_for` declined -- docs stories and
errands always took the tar path.

**And none of it ran.** `available()` is `GEMINI_API_KEY and
AGENTOS_ASSIST == "1"`. The key is mounted; the flag was set nowhere in
this repository, so the assessor, the investigator, the answerer, the
verifier assist and the scout all returned early without a log line.
Every verdict this deployment has produced passed on ONE key -- the
mechanical checks, written by the same model whose work they check.
"""
from __future__ import annotations

import ast
import asyncio
import io
import pathlib
import tarfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _tar(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


# ── A · the environment's identity ─────────────────────────────────────

def test_a_file_beside_the_setup_changes_the_environments_identity():
    """`pip install -r requirements/$(cat .agentos/python-version).txt`
    is the shape. Before this, that file was invisible to the hash."""
    from kernel.verifier import env_payload

    base = {"verify/.agentos/setup.sh": b"#!/bin/sh\npip install -r $(cat .agentos/v)\n"}
    one = env_payload.collect(_tar({**base, "verify/.agentos/v": b"a.txt"}), [""])
    two = env_payload.collect(_tar({**base, "verify/.agentos/v": b"b.txt"}), [""])

    assert env_payload.key("img", one) != env_payload.key("img", two), (
        "a file under .agentos/ changed and the environment's identity "
        "did not, so the install is skipped and the run is verified "
        "against an environment its manifest would not build")


def test_the_named_inputs_still_count():
    """Widening must not lose what was already covered."""
    from kernel.verifier import env_payload

    a = env_payload.collect(_tar({"verify/requirements.txt": b"fastapi"}), [""])
    b = env_payload.collect(_tar({"verify/requirements.txt": b"flask"}), [""])

    assert env_payload.key("img", a) != env_payload.key("img", b)


def test_source_changes_still_do_not_rebuild():
    """The saving the hash exists for: hashing the whole tree would
    rebuild the environment on every edit."""
    from kernel.verifier import env_payload

    m = {"verify/requirements.txt": b"fastapi"}
    a = env_payload.collect(_tar({**m, "verify/app.py": b"x = 1"}), [""])
    b = env_payload.collect(_tar({**m, "verify/app.py": b"x = 2"}), [""])

    assert env_payload.key("img", a) == env_payload.key("img", b)


# ── B · the cold rebuild ───────────────────────────────────────────────

@pytest.mark.parametrize("built,generation,due", [
    (None, 1, False),     # see test_an_undated_environment_is_not_due
    (1, 1, False),
    (1, 5, False),
    (1, 6, True),         # N generations on
    (1, 40, True),
])
def test_cold_due(built, generation, due):
    from kernel.verifier.rebuild import cold_due

    assert cold_due({"built_at_generation": built}, generation) is due


def test_an_undated_environment_is_not_due():
    """I HAD THIS BACKWARDS and eight tests said so.

    The first version treated NULL as due, by analogy with
    `volume.stale` calling an unknown STAMP stale. The analogy does not
    hold. An unknown stamp means we cannot say WHAT the environment is
    -- a correctness question. An unknown generation means we cannot say
    HOW OLD it is, while `env_key` still says exactly what it is -- a
    staleness question, and a far weaker one.

    Treating it as due forced a rebuild for every execution in flight
    when this shipped, and made 5.10's "an unchanged manifest costs no
    Job" false for any row predating the column. Eight tests in
    `test_the_environment_waits_for_its_manifest.py` failed, correctly,
    and that is how the direction was found to be wrong.

    The window closes on its own: any manifest change rebuilds and
    stamps, so an undated environment is dated the first time it is
    genuinely rebuilt, and cold rebuilds start counting from there.
    """
    from kernel.verifier.rebuild import cold_due

    assert cold_due({"built_at_generation": None}, 9999) is False


def test_a_cold_rebuild_is_not_swallowed_by_the_unchanged_key():
    """`_refresh` returns early when the key matches -- which is every
    cold rebuild, unless `cold` is part of that condition."""
    tree = ast.parse(_src("kernel/verifier/rebuild.py"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_refresh":
            continue
        # STRUCTURAL, not a string. The first draft pinned the exact
        # source text and failed on `ast.unparse`'s own parenthesising
        # ("... and (not cold)") -- a guard that breaks on formatting
        # tells you nothing about the property.
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            test = ast.unparse(inner.test)
            if "stale" not in test:
                continue
            assert "cold" in test, (
                f"the unchanged-key early return does not consider "
                f"`cold` ({test}), so no cold rebuild reaches a Job")
            return
        raise AssertionError("no stale check in _refresh; re-point this")
    raise AssertionError("_refresh is gone; re-point this check")


def test_the_job_is_told_to_ignore_the_stamp_when_cold():
    """`prepare_script` step 1 re-asks the VOLUME's stamp and skips the
    install when it matches -- correct for a stale row, and exactly what
    a cold rebuild must not hit."""
    body = _src("kernel/verifier/rebuild.py")

    assert "stamped=None if cold else state[\"env_key\"]" in body, (
        "the cold rebuild passes the stamp it is trying to ignore, so "
        "the Job starts, finds it current, and installs nothing")


def test_only_a_real_build_stamps_the_clock():
    """`built()` is called for a cache HIT too. Stamping there would
    reset the counter without rebuilding, and the cold rebuild would
    never come due."""
    body = _src("kernel/verifier/verify_state.py")

    assert "COALESCE(" in body and "built_at_generation" in body, (
        "a cache hit overwrites the build generation")


# ── C · what the assessor is shown ─────────────────────────────────────

def test_the_assessor_leads_with_what_the_story_changed():
    from kernel.verifier.assess import assess
    import inspect as i

    assert "changed" in i.signature(assess).parameters
    assert "WHAT THIS STORY CHANGED" in _src("kernel/verifier/assess.py")


def test_the_diff_is_computed_where_the_history_is():
    """`git archive` carries no `.git`, so the verification pod has files
    at one commit and no history. The clone is in the execution pod."""
    from kernel.workspace.podfs import OPS
    from kernel.workspace.podfs_auth import READ_ONLY_OPS

    assert "changed" in OPS, "podfs cannot answer what a story changed"
    assert "changed" in READ_ONLY_OPS, (
        "`changed` is not declared read-only, so the Verifier's "
        "read-only token is refused it")


def test_the_diff_is_cumulative_from_the_branch_point():
    """A story has many rounds; the assessor judges the finished work.
    And merge-base rather than the lane head, so a trunk that moved
    mid-story is not reported as this story's work."""
    body = _src("kernel/workspace/podfs.py")

    assert "merge-base" in body
    assert "diff" in body and "--stat" in body


def test_a_missing_execution_pod_falls_back_rather_than_failing():
    """The pod may be gone on a re-verification after a long park."""
    from kernel.verifier.inspect_tree import changed_for

    class Boom:
        async def fetch(self, *a):
            raise RuntimeError("gone")

        async def fetchval(self, *a):
            raise RuntimeError("gone")

    assert asyncio.run(changed_for(Boom(), None, 1)) == ""


@pytest.mark.parametrize("method,marker", [
    ("list_files", "TREE:"),
    ("read_file", "truncated"),
])
def test_the_tools_say_when_they_truncate(method, marker):
    """Silent truncation is the defect, not the cap. A model that cannot
    see the edge of what it was shown answers from a partial view and
    believes it is whole."""
    from kernel.verifier import inspect_tree

    sent = []

    async def fake(host, command, timeout_s, port=None):
        sent.append(command)
        return {"output": "", "exit_code": 0}

    import kernel.verifier.shell as shell
    original, shell.run = shell.run, fake
    try:
        asyncio.run(getattr(inspect_tree.PodTree("h"), method)("x"))
    finally:
        shell.run = original

    assert marker in sent[0], (
        f"{method} cannot tell the model it truncated")


def test_run_says_it_is_showing_the_tail():
    from kernel.verifier import inspect_tree

    async def fake(host, command, timeout_s, port=None):
        return {"output": "y" * 50_000, "exit_code": 0}

    import kernel.verifier.shell as shell
    original, shell.run = shell.run, fake
    try:
        got = asyncio.run(inspect_tree.PodTree("h").run("pytest"))
    finally:
        shell.run = original

    assert "showing the last" in got


# ── D · work with nothing to install ───────────────────────────────────

def test_a_no_manifest_run_still_seeds_the_tree_on_kubernetes():
    """No manifest meant no Job, so no tree on the volume and no
    `env_key` -- and `warm_for` declines without one. The class of work
    with nothing to install was the class that could never be verified
    in its pod."""
    body = _src("kernel/verifier/prepare_volume.py")

    assert "if not discovered and not claim_capable(driver):" in body, (
        "a no-manifest run skips the Job on every substrate again, so "
        "docs stories and errands cannot reach the warm pod")


def test_docker_still_skips_it():
    """There is no claim there to put a tree on."""
    body = _src("kernel/verifier/prepare_volume.py")

    assert "claim_capable" in body


def test_a_job_with_no_commands_still_stamps_an_empty_environment():
    """What makes D work: the script wipes, makes and stamps even with
    nothing to install, and the init container seeds the tree."""
    from kernel.verifier import prepare_script

    script = prepare_script.script("abc123", [])

    assert "rm -rf" in script
    assert "abc123" in script


# ── and the flag that kept all of it dark ──────────────────────────────

def test_the_assist_flag_is_set_on_both_deployments():
    """`available()` is `GEMINI_API_KEY and AGENTOS_ASSIST == "1"`. The
    key was mounted and the flag was set nowhere, so the assessor, the
    investigator, the answerer, the verifier assist and the scout all
    returned early -- silently, for the life of both Deployments."""
    manifest = _src("deploy/k8s/conductor.yaml")

    assert manifest.count("- name: AGENTOS_ASSIST") == 2, (
        "AGENTOS_ASSIST is not on both the conductor and the verifier, "
        "so the LLM half of verification is switched off and nothing "
        "says so")


def test_the_gate_still_needs_both_halves():
    """The flag alone must not be enough: a deployment with no key
    should not start calling a model it cannot reach."""
    body = _src("kernel/harness/models/quick.py")

    assert "GEMINI_API_KEY" in body and "AGENTOS_ASSIST" in body
