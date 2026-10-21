"""A check must name the mount it runs in.

THE OBSERVED FAULT, from a plan that was approved and ran:

    .venv/bin/pytest repos/Pilot-Wave-Technology__multi_agent_dummy_repo/tests

Two frames of reference in one command. `.venv/bin/pytest` is relative to the
MOUNT (setup puts `.venv/` inside the tree); `repos/<owner>__<name>/tests` is
relative to the workspace ROOT. No cwd makes both halves true.

With one repo it is wrong in a way that usually still works, which is why it
survived. With two, `verify.environment_for` finds no `cd`, resolves no target,
and falls back to alphabetical order — so the check runs the WRONG repository's
interpreter and the failure looks like broken tests.
"""
from __future__ import annotations

from kernel.tools.check_paths import offending_checks, refusal_for

REPO = "repos/Pilot-Wave-Technology__multi_agent_dummy_repo"


def test_the_observed_command_is_refused_with_the_right_fix():
    bad = offending_checks([{"command": f".venv/bin/pytest {REPO}/tests"}])
    assert len(bad) == 1
    assert bad[0]["mount"] == REPO
    assert bad[0]["suggestion"] == f"cd {REPO} && .venv/bin/pytest tests"


def test_flags_survive_the_rewrite():
    """The suggestion has to be runnable, not merely shaped right."""
    bad = offending_checks(
        [{"command": f'.venv/bin/pytest {REPO}/tests -k "divide and zero"'}])
    assert bad[0]["suggestion"] == (
        f'cd {REPO} && .venv/bin/pytest tests -k "divide and zero"')


def test_a_check_that_names_its_mount_is_accepted():
    assert offending_checks(
        [{"command": f"cd {REPO} && .venv/bin/pytest tests"}]) == []


def test_a_check_that_touches_no_repo_directory_is_left_alone():
    """This refuses ONE thing. It is not a judge of whether a check is good —
    a validator that makes judgements is one people learn to route around."""
    for cmd in ("test -f README.md", "python -c 'import json'",
                "npm test", "cd . && ls"):
        assert offending_checks([{"command": cmd}]) == [], cmd


def test_a_subshell_cd_counts():
    """`( cd x && y )` is the same promise, written defensively."""
    assert offending_checks(
        [{"command": f"( cd {REPO} && .venv/bin/pytest tests )"}]) == []


def test_a_cd_somewhere_else_still_counts_as_naming_a_target():
    """The convention is "say where you run", not "run in the mount you
    mention". A check may legitimately cd into one repo and read another's
    fixture — the Verifier just needs the target named so PATH is ordered."""
    assert offending_checks(
        [{"command": f"cd repos/a__b && diff out.txt {REPO}/expected.txt"}]) == []


def test_the_refusal_names_every_offender_and_is_actionable():
    """A refusal a model cannot act on is a loop."""
    text = refusal_for([
        {"command": f".venv/bin/pytest {REPO}/tests"},
        {"command": f"cat {REPO}/README.md"},
        {"command": "test -f go.mod"},
    ])
    assert "2 of your checks" in text          # the third is fine
    assert f"cd {REPO} && .venv/bin/pytest tests" in text
    assert "alphabetically" in text            # says WHY, not just what
    assert "Re-register the plan" in text      # says what to do next


def test_no_checks_and_empty_checks_are_silent():
    assert refusal_for(None) == ""
    assert refusal_for([]) == ""
    assert refusal_for([{"command": "   "}]) == ""


def test_plain_strings_are_accepted_as_well_as_dicts():
    """`register_plan` normalises to dicts, but the validator is also useful
    against a raw list and must not care."""
    assert len(offending_checks([f".venv/bin/pytest {REPO}/tests"])) == 1


def test_a_path_that_merely_resembles_a_mount_is_not_flagged():
    """`repos/x__y` is the layout. `myrepos/x` and `repos/plain` are not, and
    flagging them would train people to ignore this."""
    for cmd in ("ls myrepos/a__b/tests", "ls repos/plainname/tests"):
        assert offending_checks([{"command": cmd}]) == [], cmd
