"""R1 — a regression check that lost its baseline must stop claiming one.

WHAT THIS IS GUARDING. `criterion_baselines` existed so that a failing
`guard` could be called DEFINITIVELY a regression: the row said it
passed before the work started. Two things then happened. Plan 5.6 (E9)
retired the pre-approval dry run because it was a full `verify_claim`
(112.5s on execution 208; execution 203 spent six Jobs on them across
five generations), and `2e65fcf8` removed the last `blocks_approval`
remnant. `questions.dryrun` was left with a reader and no writer, so
`adopt.py` inserted zero baselines on every approval and said nothing,
and the plan-approval card told every person "Dry-run still running".

The property is genuinely lost and the loss is measured: execution 211
reached DONE with `claim 27 passed` having written no code, because
`api/main.py` and `tests/test_api.py` were already on the branch from
execution 210. A `decorative` check — the exact case the retired gate
caught.

These are source-inspection tests on purpose. The failure mode is not a
crash: every function involved degrades to an empty dict and carries on
looking correct. Only reading the source can tell you the claim is
unbacked, so that is what is asserted.

WHEN E19 LANDS, these tests are what you edit. Restoring a baseline is
allowed — from the warm verification pod running the goal criteria
against the BASE COMMIT — and then a writer and a READER arrive
together, which is what `test_a_baseline_writer_may_not_return_alone`
is here to insist on.
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
ADOPT = REPO / "kernel" / "worker" / "adopt.py"
CARDS = REPO / "kernel" / "assistant" / "cards.py"
PARK_CARDS = REPO / "app" / "services" / "kernel_park_cards.py"

# Where a reader could plausibly hide. Not the whole tree: `migrations/`
# must keep the table's DDL and grants, `infra/e0/ci` truncates it
# between corpus runs, and `docs/` is the record of why.
SOURCE_DIRS = ("kernel", "app")


def _sources():
    for d in SOURCE_DIRS:
        for p in sorted((REPO / d).rglob("*.py")):
            if "__pycache__" in p.parts or p.name.startswith("test_"):
                continue
            yield p, p.read_text(encoding="utf-8")


def _executable_text(source: str) -> str:
    """The file with its comments and docstrings taken out.

    A WORD-SEARCH WOULD FAIL ON ITS OWN EVIDENCE. The whole point of
    this change is that three files now carry long comments explaining
    why `questions.dryrun` must not be read; grepping for the word
    matches those and reports the fix as the bug. So the assertions run
    against what the file DOES: every name it mentions and every string
    literal that is not a docstring — which still catches a column named
    inside a SQL string, the only way this could come back unnoticed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:                                     # pragma: no cover
        return source
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            first = body[0] if body else None
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                out.append(node.value)
        elif isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            out.append(node.name)
    return chr(10).join(out)


def test_adopt_no_longer_claims_a_failure_is_definitively_a_regression():
    """The docstring is the claim. It was load-bearing and is now false.

    A guard failing at verification is indistinguishable from a guard
    that never passed, because nothing measured the pre-work tree.
    """
    text = ADOPT.read_text(encoding="utf-8")
    assert "DEFINITIVELY a regression only because" not in text
    assert "INSERT INTO criterion_baselines" not in text, (
        "adopt.py is inserting baselines again; if the data source is "
        "real, say which pass measured it and delete this assertion")


def test_adopt_says_what_would_restore_the_baseline():
    """A removed property with no note is just a silent downgrade.

    E19 (the warm verification pod) is the named repair, and 5.6's
    price is named too so nobody 'restores' it with a second dry run.
    """
    text = ADOPT.read_text(encoding="utf-8")
    assert "E19" in text
    assert "base commit" in text.lower()
    assert "112.5s" in text, (
        "the cost that retired the dry run is what stops somebody "
        "reintroducing one to get the baseline back")


def test_questions_dryrun_has_no_reader_left():
    """A column nothing writes must be read by nothing.

    Both readers returned `{}` for ever: `adopt.py` relabelled nothing
    while looking like it still settled labels, and `dryrun_of` rendered
    a permanent 'still running' line on every approval card.
    """
    offenders = [str(p.relative_to(REPO)) for p, s in _sources()
                 if "dryrun" in _executable_text(s)]
    assert offenders == [], (
        f"`questions.dryrun` is being read again by {offenders}; nothing "
        f"has written it since plan 5.6 retired the dry run")


def test_the_approval_card_carries_no_dry_run_section():
    """`dryrun_of` is gone and no card may grow a replacement.

    The chat template keys an italic 'Dry-run still running — you can
    approve now; it is advisory' on `card.dryrun.status == 'pending'`,
    which is exactly what an empty column produced. Absent is correct;
    the guards in index.html already handle a missing key.
    """
    assert "dryrun_of" not in _executable_text(
        CARDS.read_text(encoding="utf-8"))
    assert "dryrun" not in _executable_text(
        PARK_CARDS.read_text(encoding="utf-8"))


def test_a_baseline_writer_may_not_return_alone():
    """The table was written and NEVER read, which is the deeper bug.

    stage-f recorded it from the other end: "`criterion_baselines` is
    written but not yet READ by a refusal ... a refusal still says
    `CRITERION_COMMAND_FAILED` for both cases." So the row that was
    supposed to make a failure legible never reached a person. A writer
    that comes back without a refusal that consults it rebuilds exactly
    that: cost, no signal.
    """
    writers, readers = [], []
    for p, s in _sources():
        rel = str(p.relative_to(REPO))
        if "INSERT INTO criterion_baselines" in s:
            writers.append(rel)
        if ("FROM criterion_baselines" in s
                or "FROM kernel.criterion_baselines" in s):
            readers.append(rel)
    assert not (writers and not readers), (
        f"{writers} writes criterion_baselines and nothing reads it. "
        f"Wire the refusal that distinguishes 'this regressed' from "
        f"'this never passed', or the rows are cost with no signal")
    assert not (readers and not writers), (
        f"{readers} reads criterion_baselines and nothing writes it — "
        f"it will return None for every criterion created from now on")


def test_the_discriminator_itself_survives():
    """`discriminate.judge` is the rule E19 feeds; it did not die with
    the phase that ran it. Deleting it would make the repair a rewrite
    rather than a wiring job."""
    text = (REPO / "kernel" / "verifier"
            / "discriminate.py").read_text(encoding="utf-8")
    assert "decorative" in text and "def judge(" in text
