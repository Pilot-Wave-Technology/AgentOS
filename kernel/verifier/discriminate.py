"""F11 — can this criterion tell the difference?

Pure functions over (label, baseline outcome). No I/O, no model, no
judgement: every rule here is a comparison between what a check is
declared to be and what it did against the tree before any work started.

THE PHASE DIED AND THIS DID NOT (plan 5.6, E9). F11's dry run ran these
rules BEFORE a person approved a plan and refused the approval on what
they said; that gate retired because measuring it cost a full
`verify_claim` per approval. The rules themselves are seconds of work in
a pod that is already warm: running the goal criteria against the base
commit at VERIFICATION time yields the same `decorative` finding for the
same reason. Recorded as available, not as a gate — and nothing wires it
yet, which is the honest state.

AND THE `blocks_approval` SHIM IS GONE. E9 could not delete it with the
gate: its only caller was `kernel/harness/park.py`, another change's
file, so it was left as a documented `return ""` rather than an import
error on every plan approval. That caller's F11 block is now removed too
(plan 5.18 item 2, the thing it said was owed), so the shim went with it.
Do not reintroduce a function here that returns a verdict on an approval:
`judge` and `summarise` below say what a criterion can prove, and saying
so is all this module is allowed to do.

The rules, and what each one is protecting against:

    goal that FAILS      ok            the ordinary case
    goal that PASSES     decorative    the bug that started F11 — a check
                                       that was already green could never
                                       have proved the work was done
    goal that PASSES
      and was carried    promote       work that is finished and must not
                                       regress IS a guard; saying so is
                                       more useful than refusing it
    guard that PASSES    ok            the ordinary case
    guard that FAILS     mislabelled   and the message is the interesting
                                       part: a check that fails before the
                                       work is what a GOAL is. Approving
                                       it as a guard dooms the story,
                                       because it can never pass
    anything ERRORS      broken        the command did not run. NOT the
                                       same finding as a bad bar, and
                                       flattening them would report a typo
                                       as a design problem
    unlabelled           uncheckable   written before F11. Reported, never
                                       guessed at

And one finding that is evidence rather than a rule, from G1b: a check
that answers IDENTICALLY against an empty tree had nothing in the code
affect its result. That warns and never blocks — see `judge`.
"""

from __future__ import annotations

# A plan cannot be approved while any of these stands. `uncheckable` is
# deliberately absent: every criterion written before F11 is one, and
# refusing them would make old contracts unapprovable to fix a problem
# they do not have.
BLOCKING = ("decorative", "mislabelled", "broken")


def _in_tree_runner(command: str, output: str) -> bool:
    """Is this a command-not-found whose missing runner LIVES IN THE TREE?

    `repos/x/.venv/bin/pytest tests/` before the venv exists is the same
    shape as `test -f WORK.md` before WORK.md exists: the work itself
    creates what the check invokes. A missing SYSTEM binary (`cargo` in
    a python image) is the opposite -- it can never appear, whatever the
    work does. The discriminator is the runner's path: relative and
    inside the tree = creatable; bare or absolute = the image's problem.
    """
    if "not found" not in (output or ""):
        return False
    text = " ".join(str(command or "").split())
    # peel a leading `cd <dir> &&` -- the runner is what comes after
    while text.startswith("cd "):
        _, _, rest = text.partition("&&")
        if not rest:
            return False
        text = rest.strip()
    runner = text.split(" ", 1)[0]
    return "/" in runner and not runner.startswith("/")


def judge(label: str, outcome: str, carried: bool = False,
          inert: str = "", command: str = "", output: str = "") -> dict:
    """One criterion's dry-run verdict, with the sentence to show a person.

    `inert` is G1b's probe: the criterion answered identically against an
    empty tree, so nothing in the code moved the result. It WARNS and
    does not block, because `test -f WORK.md` before WORK.md exists is
    inert and is a perfectly good goal, while `python3 -m pytest` with no
    pytest is inert and can never pass — and nothing can separate them.
    Blocking would refuse the first in order to catch the second.
    """
    if label == "unlabelled":
        v = _v("uncheckable",
               "no label, so there is nothing to compare this against. "
               "Say whether it is a goal (the work) or a guard (what must "
               "not break).")
    elif outcome == "error":
        if label == "goal" and _in_tree_runner(command, output):
            # The goal invokes a runner the WORK will create (an in-tree
            # .venv/bin/pytest, node_modules/.bin/vitest). Blocking this
            # is the chicken-and-egg that taught an assistant to strip
            # checks: the plan's own first step creates the environment,
            # but nothing can be committed before the plan is approved.
            # If the work never creates the runner, verification refuses
            # for ever -- the enforcement is not lost, only deferred to
            # the party that can act on it.
            v = _v("ok",
                   "the runner this invokes does not exist yet and lives "
                   "inside the tree — the work is expected to create it. "
                   "If it never does, verification will refuse for ever.")
        else:
            v = _v("broken",
                   "this did not run at all against the current tree. Fix "
                   "the command — a check that errors proves nothing "
                   "either way.")
    elif outcome == "flake":
        v = _v("broken",
               "this disagreed with itself before the work started, so it "
               "cannot be a bar. Make it deterministic first.")
    elif label == "goal":
        if outcome == "fail":
            v = _v("ok", "fails now, must pass when the work is done.")
        elif carried:
            v = _v("promote",
                   "this passes now because it is already done. Carried "
                   "forward as a GUARD: it must keep passing, and it is no "
                   "longer evidence that anything new happened.")
        else:
            v = _v("decorative",
                   "this ALREADY PASSES, before any work. It cannot show "
                   "the work was done, so the story could be verified "
                   "having done nothing. Make it specific to what you are "
                   "about to build.")
    elif label == "guard":
        if outcome == "pass":
            v = _v("ok", "passes now, and must keep passing.")
        else:
            v = _v("mislabelled",
                   "a guard is something that ALREADY WORKS and must not "
                   "break — and this is failing before the work starts. As "
                   "a guard it dooms the story, because it can never pass. "
                   "If fixing it is part of the job, it is a goal.")
    else:
        v = _v("uncheckable", f"unknown label {label!r}.")
    return _with_inert(v, inert)


def _v(verdict: str, note: str) -> dict:
    return {"verdict": verdict, "note": note, "blocks": verdict in BLOCKING}


def _with_inert(v: dict, inert: str) -> dict:
    """Attach the empty-tree finding without changing the verdict.

    Separate from the verdict on purpose: `inert` is evidence about the
    check, and the verdict is the rule applied to it. A `goal` that
    fails is still `ok` by the rule — what changes is that a person is
    told the failure had nothing to do with their code.
    """
    if not inert:
        return v
    return {**v, "inert": True,
            "note": v["note"] + "  ⚠ " + inert}


def summarise(results: list[dict]) -> dict:
    """What the whole set says, for the line above the detail."""
    blocking = [r for r in results if r.get("blocks")]
    goals = [r for r in results if r.get("label") == "goal"]
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    if blocking:
        headline = (f"{len(blocking)} check(s) cannot be approved as "
                    f"written — see below")
    elif not results:
        headline = ("NO CHECKS. Nothing will define done for this story, "
                    "and it can never be verified.")
    elif not goals:
        # Every check is a guard, which means nothing here can show the
        # work happened — only that nothing broke. Worth saying out loud
        # even though it does not block: a person may have meant it.
        headline = ("every check is a guard. Nothing here can show the "
                    "work was done — only that nothing broke.")
    else:
        headline = (f"{len(goals)} goal(s) fail now and must pass; "
                    f"{len(results) - len(goals)} guard(s) pass now and "
                    f"must keep passing.")
    # G1b rides on the headline rather than only on the row, because a
    # warning attached to one line of a five-line list is a warning
    # people scroll past.
    n_inert = len([r for r in results if r.get("inert")])
    if n_inert:
        headline += (f"  ⚠ {n_inert} of them answer the same against an "
                     f"EMPTY tree — nothing in the code moved the result.")
    return {"headline": headline, "blocked": bool(blocking),
            "inert": n_inert, "counts": counts}
