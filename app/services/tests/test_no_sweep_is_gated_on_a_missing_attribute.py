"""Three sweeps that never ran, behind a guard that is always true.

    if getattr(driver, "name", "") != "kubernetes":
        return []

NO DRIVER IN THIS TREE DEFINES `.name` -- not `KubernetesDriver`, not
`DockerDriver`, not the `Driver` base. So the getattr returns `""`, the
comparison is true on every substrate, and everything below it is
unreachable. Confirmed against the live conductor:

    instance class: KubernetesDriver
    .name         : <MISSING>
    guard passes  : False

THREE SWEEPS WERE BEHIND IT: `reclaim.verification_reapable`,
`ephemeral.sweep_ephemeral`, `jobs_sweep.sweep_jobs`. All three listed,
triaged and deleted correctly; none of it had ever executed.

AND IT WAS ALREADY WRITTEN DOWN. `unscheduled.py`'s header says, in as
many words: "Three other sweeps in this package spell the same gate
... and that idiom is BROKEN ... so the guard is always true and the
sweep behind it returns `[]` unconditionally." Somebody found it,
warned their own module about it, and left the three it names alone --
the same shape as plan 5.19 documenting E19 and E19 never being built.

WHY IT SURFACED NOW. Nothing created verification claims before E19, so
a reaper that collected none was indistinguishable from one that worked.
E19 makes one per coding story. Measured: `verify-ws-218` still Bound
thirteen hours after execution 218 reached DONE, with `verify-ws-219`
beside it -- a 10GiB leak per story against the 120-claim ceiling W1
records.

A DEAD SWEEP IS INVISIBLE BY CONSTRUCTION, which is why this file is a
static check rather than a behavioural one. Its whole symptom is that
nothing happens, on a cluster where nothing happening is the normal
case, and the log line it would have written is the one that never
appears.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]

# The sweeps that were behind the broken guard, and what each collects.
SWEEPS = (
    ("kernel/conductor/reclaim.py", "verification_reapable",
     "the verification pod and its claim"),
    ("kernel/conductor/ephemeral.py", "sweep_ephemeral",
     "ephemeral claims past their age"),
    ("kernel/conductor/jobs_sweep.py", "sweep_jobs",
     "verifier Jobs whose execution has finished"),
)


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _code(rel: str) -> str:
    """Source with comments and docstrings gone.

    The fixes EXPLAIN the broken idiom at length, so a plain substring
    search would match the explanation and pass on prose -- a trap this
    suite has already fallen into four times.
    """
    tree = ast.parse(_src(rel))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            if ast.get_docstring(node, clean=False) is not None:
                node.body = node.body[1:]
    return ast.unparse(tree)


def test_no_driver_defines_name_so_the_idiom_cannot_work():
    """The premise. If a driver ever grows `.name`, the old guard starts
    working and this file's reasoning needs revisiting rather than its
    assertions quietly passing."""
    from kernel.driver import Driver
    from kernel.drivers.docker import DockerDriver
    from kernel.drivers.kubernetes import KubernetesDriver

    for cls in (Driver, DockerDriver, KubernetesDriver):
        assert not hasattr(cls, "name"), (
            f"{cls.__name__} now defines `.name`; the "
            f"`getattr(driver, 'name', '')` guard would start working "
            f"and this file's premise no longer holds")


@pytest.mark.parametrize("rel,func,collects", SWEEPS)
def test_the_sweep_is_not_gated_on_a_missing_attribute(rel, func, collects):
    """THE GUARD ITSELF. `getattr(driver, "name", ...)` anywhere in
    these files means the sweep is dead again."""
    assert 'getattr(driver, "name"' not in _code(rel), (
        f"{func} is gated on `driver.name`, which no driver defines, so "
        f"it returns [] on every tick and {collects} is never collected")


@pytest.mark.parametrize("rel,func,collects", SWEEPS)
def test_the_sweep_asks_a_question_the_driver_answers(rel, func, collects):
    """`claim_capable` reads `CLAIM_JOB_MODEL`, which the drivers
    actually declare -- Kubernetes "job", everything else "none"."""
    code = _code(rel)

    assert "claim_capable" in code, (
        f"{func} no longer asks `claim_capable`; whatever replaced it "
        f"must be an attribute the drivers declare, or {collects} stops "
        f"being collected and nothing says so")


def test_the_predicate_separates_the_substrates_it_is_meant_to():
    """True where there are claims to reap, false where the criteria are
    containers the driver removes itself."""
    from kernel.verifier.handover import claim_capable

    class Kubernetes:
        CLAIM_JOB_MODEL = "job"

    class Docker:
        pass

    assert claim_capable(Kubernetes()) is True
    assert claim_capable(Docker()) is False


def test_the_warning_that_was_ignored_is_still_there():
    """`unscheduled.py` named the idiom and the three sweeps it broke.
    Keeping the note keeps the archaeology: it is the reason this file
    exists, and deleting it once the code is fixed would lose why."""
    assert "idiom is" in _src("kernel/conductor/unscheduled.py")
