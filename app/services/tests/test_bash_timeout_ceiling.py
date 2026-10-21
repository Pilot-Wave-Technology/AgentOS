"""F3 · nothing may ask the sandbox for a timeout it will refuse.

THE REGRESSION THIS CLOSES, found live in §1 of the test plan. Ec1a-e
added `kernel/bridge/limits.py` to fix the opposite defect — `timeout`
was absent from `bash`'s schema, so no model could set one and every
foreground command died at the 30s default — and introduced
`BASH_MAX_S = 120` as a REFUSAL rather than a clamp, deliberately, so a
model asking for 600 is told to use `bash_background` instead of
retrying a slow command.

What it did not do is check what already asked for more. Two system
callers did, both at 600:

  * `kernel/harness/selfcheck.py` — the checks `propose_completion` runs.
    So EVERY story's self-check failed with
    `[E_BAD_ARGUMENT] timeout 600s exceeds the 120s ceiling`, identically,
    whatever the work was. The story could not complete, and the message
    reads as the checks being broken rather than the request being out of
    range.
  * `kernel/worker/envsetup.py` — every environment setup, the same way.

A ceiling and the numbers measured against it, declared in three files.
The fix is that the callers IMPORT the ceiling; this is the guard that
they keep doing so.

THE MODEL-FACING CEILING IS NOT WEAKENED. Raising BASH_MAX_S would have
unblocked it too, and would have thrown away the reason it exists: a
foreground command holds the leg, and `bash_background` is the answer
for anything long. A check that genuinely needs more than the ceiling
needs backgrounding, not a bigger number.
"""
from __future__ import annotations

import io
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]

# Every module that hands a `timeout` to a bash call over the bridge.
# Derived below rather than trusted: the point is that a NEW one cannot
# appear without this failing.
# BOTH SELF-CHECKS LEFT THIS SET. Neither runs a command criterion any
# more: artifact checks are answered from the manifest, and command
# checks are re-run only by the Verifier, in a clean container, when the
# platform switch is on. That removed the original offender rather than
# raising the ceiling for it, which is the outcome this file wanted.
KNOWN = {"kernel/worker/envsetup.py", "kernel/harness/executor.py",
         "kernel/verifier/services.py"}


def _sources():
    for rel in ("kernel/harness", "kernel/worker", "kernel/verifier",
                "kernel/bridge"):
        for path in (ROOT / rel).glob("**/*.py"):
            if "__pycache__" in str(path):
                continue
            yield (str(path.relative_to(ROOT)).replace("\\", "/"),
                   io.open(path, encoding="utf-8").read())


def test_the_two_callers_import_the_ceiling_rather_than_restate_it():
    """THE FIX. A number that must equal another number, written out in
    a second file, is the shape this project keeps paying for."""
    # envsetup no longer has a CEILING at all -- E14 made its install a
    # job whose budget the pod enforces -- but it does have a patience,
    # and the rule being guarded is unchanged: IMPORT the number, never
    # restate it.
    #
    # `harness/selfcheck.py` USED TO BE THE OTHER HALF OF THIS PAIR, and
    # is gone from it because it no longer sends a timeout anywhere: the
    # self-check measures artifact criteria from the manifest and does
    # not run command criteria at all. The pairing is kept as a list of
    # one rather than deleted, because the rule it guards applies to
    # whatever is added next.
    for rel, const, ceiling in (
            ("kernel/worker/envsetup.py", "SETUP_WAIT_S",
             "jobs_client.DEFAULT_WAIT_S"),):
        src = io.open(ROOT / rel, encoding="utf-8").read()
        assert "from kernel.bridge" in src, rel
        assert ceiling in src, f"{rel}: does not import {ceiling}"
        assert re.search(rf"^{const}\s*=\s*{re.escape(ceiling)}\s*$",
                         src, re.M), f"{rel}: {const} is a literal again"


def test_no_bash_call_asks_for_more_than_the_sandbox_allows():
    """THE GENERAL FORM, so the next one is caught by a test rather than
    by a story that cannot finish. Reads every literal timeout handed to
    a bash call and measures it against the ceiling the sandbox
    enforces."""
    from kernel.bridge.limits import BASH_MAX_S

    offenders = []
    for rel, src in _sources():
        if rel.endswith("limits.py"):
            continue
        for literal in re.findall(r'"timeout":\s*(\d+)', src):
            if int(literal) > BASH_MAX_S:
                offenders.append(f"{rel}: timeout {literal}")
    assert not offenders, (
        f"{offenders} exceed BASH_MAX_S={BASH_MAX_S}; the sandbox refuses "
        f"them with E_BAD_ARGUMENT and the caller sees every command fail "
        f"identically, whatever it was trying to do. Use bash_background "
        f"for anything genuinely longer.")


def test_the_sandbox_actually_accepts_what_the_callers_send():
    """The end-to-end version of the two above: take each caller's
    constant and put it through the function the sandbox applies."""
    from kernel.bridge.limits import BASH_MAX_S, bash_timeout, job_budget

    # The self-check's constant used to be the subject here. It no longer
    # has one -- it runs no commands -- so what is left to assert is that
    # the ceiling itself round-trips, which is what every remaining
    # caller is measured against.
    assert bash_timeout({"timeout": BASH_MAX_S}) == BASH_MAX_S
    # And the install path, which now goes to the other validator: it
    # declares no budget, so what it gets is the default, and the
    # default has to be acceptable to the thing that enforces it.
    assert job_budget({}) == job_budget({"budget_s": job_budget({})})


def test_the_ceiling_is_still_a_refusal_and_not_a_clamp():
    """Ec1a-e chose refusal deliberately: silently clamping 600 to 120
    hands back `E_TIMEOUT: no output within 120s`, which reads as a slow
    command rather than an out-of-range request — so the model retries
    the same thing instead of reaching for `bash_background`."""
    from kernel.bridge.limits import BASH_MAX_S, BadTimeout, bash_timeout

    with pytest.raises(BadTimeout) as caught:
        bash_timeout({"timeout": BASH_MAX_S + 1})
    assert "bash_background" in str(caught.value), (
        "the refusal does not name the way out, so the caller has nowhere "
        "to go")


def test_the_model_facing_ceiling_was_not_raised_to_paper_over_this():
    """The tempting fix, and the wrong one. A foreground command holds
    the leg; that is what the ceiling is for."""
    from kernel.bridge.limits import BASH_MAX_S

    assert BASH_MAX_S == 120, (
        "the ceiling moved. If that is deliberate, say why here — raising "
        "it to accommodate a long check trades the reason it exists for "
        "the convenience of not backgrounding one command")


# ── the harness's own setup is not a model request ───────────────────────

def test_env_setup_is_no_longer_bounded_by_a_foreground_ceiling():
    """F4, which was parked, then bit, then was fixed with a bigger
    number, and is now fixed with a different shape.

    `BASH_MAX_S` exists to push the MODEL towards `bash_background` when
    it asks for a long foreground call. `env_setup` is not the model
    asking: it is the harness installing a story's declared dependencies
    before the model gets a turn, and `pip install -r requirements.txt`
    on a cold container legitimately exceeds two minutes.

    Applying the model's ceiling to it cost a whole story -- KER25's fix
    run parked with "workspace setup failed -- [requirements.txt] exit=1
    bridge error: TimeoutError", which reads as a broken manifest or a
    dead network and was neither. A 600s ceiling of its own moved that
    cliff to a monorepo instead of removing it. E14 removed it: the
    install is a job, the budget is enforced in the pod, and the harness
    holds a thread for a poll."""
    from kernel.bridge.limits import BASH_MAX_S, BadTimeout, JOB_MAX_S

    # The model's ceiling is unchanged, and there is no longer a flag
    # that lifts it.
    assert JOB_MAX_S > BASH_MAX_S
    for args in ({"timeout": 600}, {"timeout": 600, "harness_setup": True}):
        try:
            from kernel.bridge.limits import bash_timeout
            bash_timeout(args)
            raise AssertionError(f"{args} was accepted")
        except BadTimeout as bad:
            assert str(BASH_MAX_S) in str(bad)


def test_envsetup_actually_starts_a_job_and_can_find_it_again():
    """A job it cannot re-attach to is a job a restart loses -- and the
    old blocking call lost one AND left it running (execution 200 ran a
    second pip against the same tree)."""
    import io
    import pathlib

    src = io.open(pathlib.Path(__file__).resolve().parents[3]
                  / "kernel/worker/envsetup.py", encoding="utf-8").read()
    assert "jobs_client.run_job(" in src
    assert "key=f\"env-setup-" in src, (
        "the start is not keyed, so a restarted worker starts a second "
        "install instead of re-attaching to the running one")
