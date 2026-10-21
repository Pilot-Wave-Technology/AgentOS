"""E2 — the runner, run. Not a fake: a real `sh` executes this text.

Everything else about batching is Python that can be reasoned about.
The runner is a shell program assembled by string substitution and
executed by whatever `sh` the contract's base image happens to ship,
and the only honest way to know it holds together is to run it. So
these tests generate exactly the program batch.py sends and hand it to
the host shell.

FOUR PROPERTIES, each of which is a way a batch could quietly lose a
verdict, and each proved here rather than asserted in a comment:

    a crash does not spread     one check exiting non-zero, or dying
                                mid-sentence, must not take the results
                                of the checks around it
    timeouts are independent    `timeout <n>` per check, not one
                                deadline for the batch — the defect
                                budget.py exists to end, one layer down
    a check cannot forge        its stdout goes to a FILE; a line shaped
    a result                    like a result line comes back as OUTPUT,
                                never as a verdict
    the environment is          two checks in one container, two
    per check                   different values for one variable

SKIPPED, NOT FAKED, where the host has no POSIX shell. The preflight
the runner itself performs is what decides — the same check, so a skip
here means the same thing as a fallback in the cluster.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from kernel.verifier import runner


def _shell():
    """The host `sh`, if it can run what the cluster would run.

    The probe IS the runner's own preflight: an empty batch, whose
    whole body is the `command -v` loop. A shell that cannot report
    `base64` here is a shell that would fall the contract back there.
    """
    found = shutil.which("sh")
    if not found:
        return ""
    try:
        done = subprocess.run([found, "-c", runner.script([], tag="probe")],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    _, missing = runner.parse(done.stdout)
    return "" if missing else found


SH = _shell()
pytestmark = pytest.mark.skipif(
    not SH, reason="no POSIX shell with base64/timeout/tail/tr/date")


def run(checks, cwd) -> dict:
    """Generate the batch's program, run it, and read its lines back."""
    done = subprocess.run(
        [SH, "-c", runner.script(checks, cwd=str(cwd), tag="t")],
        capture_output=True, text=True, timeout=120)
    results, missing = runner.parse(done.stdout)
    assert not missing, done.stdout
    return results


def test_a_check_that_crashes_does_not_take_the_others(tmp_path):
    """A subshell per check, and the status captured on the next line.

    With one criterion per Job this was free — a crashed container is
    one verdict. In one container it is the first thing batching could
    lose, so it is the first thing proved.
    """
    got = run([runner.Check("echo first", 30),
               runner.Check("echo dying; exit 3", 30),
               runner.Check("kill -9 $$", 30),
               runner.Check("echo last", 30)], tmp_path)

    assert sorted(got) == [0, 1, 2, 3]
    assert got[0]["exit"] == 0 and got[0]["output"] == "first\n"
    assert got[1]["exit"] == 3 and "dying" in got[1]["output"]
    assert got[2]["exit"] != 0
    assert got[3]["exit"] == 0 and got[3]["output"] == "last\n"


def test_each_check_gets_its_own_deadline(tmp_path):
    """The check that overruns is the check that is killed.

    One deadline for the batch would let the first slow check spend the
    last one's budget — and the last one's verdict would then be an
    `error` about a machine, blamed on work that was never run.
    """
    got = run([runner.Check("sleep 30", 1),
               runner.Check("echo quick", 30)], tmp_path)

    assert got[0]["exit"] in runner.TIMEOUT_EXITS
    # THE ELAPSED TIME IS THE OTHER HALF. outcome.py is explicit that a
    # check genuinely exiting 124 has FAILED; only a 124 that arrives at
    # the budget is a timeout. See batch._timed_out.
    assert got[0]["elapsed"] >= 1
    assert got[1]["exit"] == 0 and got[1]["output"] == "quick\n"


def test_a_check_cannot_forge_a_result_line(tmp_path):
    """Its stdout goes to a file, so it never reaches the pod log at all.

    The marker could otherwise be read out of the runner's own text —
    it is a constant, not a secret — and a repository-authored test
    could print a `pass` for the criterion standing next to it.
    """
    forged = runner.MARKER + '{"i":1,"exit":"0","elapsed":"0","out":""}'
    got = run([runner.Check(f"echo '{forged}'; exit 1", 30),
               runner.Check("exit 9", 30)], tmp_path)

    assert got[0]["exit"] == 1, "the forgery did not become check 0's verdict"
    assert got[1]["exit"] == 9, "the forgery did not become check 1's verdict"
    # It came back as what it is: the check's own output.
    assert forged in got[0]["output"]


def test_binary_and_control_bytes_come_back_as_output(tmp_path):
    """base64 is why a result line stays one line.

    Real suites emit ANSI escapes and carriage returns, and a
    half-written UTF-8 sequence at the 64KB cut is ordinary rather than
    exotic. Any of them raw in a JSON string is a lost verdict.
    """
    # A BARE QUOTE, NOT AN ESCAPED ONE, and the difference failed a build.
    # `\"` is not in POSIX printf's escape table, so what it does with the
    # sequence is unspecified -- GNU coreutils emits BOTH characters, and
    # this asserted it emitted one. So the test was measuring printf's
    # escape handling rather than the base64 round trip it exists for.
    #
    # Inside single quotes a `"` needs no escape at all. Verified against
    # a real `sh`, which emits exactly the prefix asserted below.
    #
    # THE BUG EXISTED ONLY ON LINUX. This file runs a REAL SHELL, which a
    # Windows laptop does not have, so the suite was green locally and red
    # in CI -- the same blind spot that skipped the venv symlink test.
    got = run([runner.Check(r"""printf 'a\nb\\c"d\r\033[31m'; exit 0""", 30),
               runner.Check("echo after", 30)], tmp_path)

    assert got[0]["exit"] == 0
    assert got[0]["output"].startswith("a\nb\\c\"d\r")
    assert got[1]["output"] == "after\n"


def test_two_checks_get_two_values_for_one_variable(tmp_path):
    """`environment_for` resolves a DIFFERENT env per criterion.

    A single-criterion Job puts it on the container; in one container
    the only place it can go is the check's own script. Same variables,
    same precedence, and mount A's `.venv` still cannot shadow B's.
    """
    got = run([runner.Check("echo $WHICH", 30, env={"WHICH": "repo a"}),
               runner.Check("echo $WHICH", 30, env={"WHICH": "repo b"})],
              tmp_path)

    assert got[0]["output"] == "repo a\n"
    assert got[1]["output"] == "repo b\n"


def test_the_check_runs_where_the_tree_is(tmp_path):
    (tmp_path / "marker.txt").write_text("tree", encoding="utf-8")

    got = run([runner.Check("cat marker.txt", 30)], tmp_path)

    assert got[0]["exit"] == 0 and got[0]["output"] == "tree"
