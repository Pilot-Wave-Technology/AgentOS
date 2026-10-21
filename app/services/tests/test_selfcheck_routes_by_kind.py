"""An artifact check is not a shell line, in the selfcheck too.

THE LOOP THIS CAUSED, on KER25 errand 1b06108e:

    approve plan -> executor works -> propose_completion -> selfcheck
    bashes `artifact[role=report,...]` -> "command not found" -> executor
    reports its goal check cannot run and asks to re-register -> GO says
    yes -> identical plan (it was already correct) -> approve -> ...

Seven generations and ~600K tokens before anyone looked at what
`checks_for` actually SELECTed. It selected `spec` and `label` and not
`kind`, so every criterion arrived looking like a command, and
`run_own_checks` dutifully shelled all of them.

The executor was right every time. It reported, accurately, that its
goal check was being executed as a shell command and failing — and was
disbelieved because the stored criterion said `kind='artifact'` and the
gateway spec, the parser and the contract were all correct. Everything
was correct except the one query that fed the thing doing the running.

`kernel/verifier/dryrun.py` had already worked this out and written it
down: shelling out an artifact check produces "an answer about the
image, dressed as an answer about the check". One fact, two places, and
only one of them knew it. (That file retired at E9 with the dry run
itself, plan 5.6; the rule outlived it, and `kernel/verifier/kinds.py`
is where it is declared now.)
"""
from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def test_checks_for_carries_the_kind():
    """It cannot be routed on what was never selected."""
    src = _read("kernel/harness/selfcheck.py")
    body = src[src.index("async def checks_for"):]
    body = body[:body.index("\nasync def ", 10)]
    assert "c.kind" in body, (
        "checks_for does not select `kind`, so every criterion reaches "
        "run_own_checks looking like a shell command")
    assert '"kind"' in body, "the kind is selected and then dropped"


def test_an_artifact_check_is_not_bashed():
    """The routing itself, now settled by SELECTION rather than by
    branch order. Nothing in the self-check shells anything out any
    more: command criteria are not re-run there at all, so an artifact
    check can no longer reach a shell by falling past a missing `if`.
    What is left to guard is that the selection stays explicit, and that
    "did this run emit a matching artifact" has exactly one
    implementation."""
    src = _read("kernel/harness/selfcheck.py")
    body = src[src.index("async def run_own_checks"):]
    body = body[:body.index("\ndef ", 10)]
    assert 'c["kind"] == "artifact"' in body, (
        "the self-check no longer selects artifact criteria by kind, so "
        "it is measuring whatever it was handed")
    assert '"bash"' not in body, (
        "the self-check shells something out again -- a command "
        "criterion belongs to the Verifier, and an artifact check sent "
        "to a shell produces 'command not found' dressed as a verdict")
    assert "artifact_kind import" in body, (
        "a second implementation of 'did this run emit a matching "
        "artifact' would answer differently the first time one changed")


def test_the_registry_and_the_selfcheck_agree():
    """Both decide what a criterion IS before running it, and they must
    not disagree. This paired the selfcheck with `dryrun.py`, which had
    the split first and is the reason the bug was legible at all; the
    dry run retired at E9 and `kinds.py` -- the registry both the
    verifier and the selfcheck resolve through -- is the surviving half
    of the pair."""
    for rel in ("kernel/verifier/kinds.py", "kernel/harness/selfcheck.py"):
        src = _read(rel)
        assert "artifact" in src and "kind" in src, rel


def test_a_failing_artifact_check_reports_what_was_emitted():
    """`evaluate` names what the run DID emit — "expected report:x, this
    run emitted method:y" — and the selfcheck must pass that through.
    "A check failed" is not actionable; the executor has one leg left to
    work out whether it used the wrong role, the wrong name, or emitted
    nothing."""
    src = _read("kernel/harness/selfcheck.py")
    body = src[src.index("async def run_own_checks"):]
    assert '"reason"' in body
