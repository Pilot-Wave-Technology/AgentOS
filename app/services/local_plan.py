r"""A local plan is the SAME object a pod's plan is — validated the same way.

WHAT THIS REPLACES. The local lane planned in `## Task DAG` YAML: an h2
heading, a fenced block, ids matching `^T\d+$`, a `phase` drawn from the
`claude_phases` registry, and one child task per entry. None of that
vocabulary exists in the kernel, whose unit is a plan of `steps` with
`checks` bound by a content hash. Two planning languages for one platform
meant a plan could not be read, approved or reasoned about the same way
depending on where the story happened to run.

WHY THE VALIDATION IS IMPORTED AND NOT REWRITTEN. Every rule below already
exists, was already argued for, and was already paid for by a real failure:

  * a check must be LABELLED `goal` or `guard` -- a `goal` check must fail
    now and pass when the work is done, which is what makes it evidence
    rather than decoration;
  * an ARTIFACT check must be able to fail -- otherwise the whole run
    happens and is then turned down by a check that was broken when it was
    written;
  * a command check must say WHERE IT RUNS. `check_paths` refuses a command
    naming a mount directory without a `cd` into one, because one global
    PATH made the first repository's `.venv` shadow the second's and the
    failure read as "the tests are broken".

That last one is also what settles multi-clone stories locally, at no cost:
a lane-qualified mount is `repos/<owner>__<name>@<branch>`, so two clones of
one repo are two directories, and a check that must `cd` into one can never
be ambiguous about which working tree it means. Re-deriving any of this for
the local lane would be re-earning it.

WHAT IS NOT HERE. No approval logic, no gate, no self-check. A local story
is self-operated: the person approves their own plan in their editor and
decides when it is done. This module answers one question -- is this a
well-formed plan? -- so that the answer is the same in both lanes.
"""

import logging

logger = logging.getLogger(__name__)


def normalise_checks(raw):
    """The kernel's own normalisation, so a check means one thing."""
    from kernel.worker.contracts import normalise
    return normalise(raw)


def refusal_for(steps, rationale: str, checks) -> str | None:
    """A sentence naming what is wrong with this plan, or None.

    Returns prose rather than raising: the caller is an HTTP endpoint whose
    answer is read by a person looking at the plan they just wrote, and the
    whole value of refusing here is that one edit fixes it.
    """
    if not steps or not isinstance(steps, list):
        return "steps must be a non-empty list"
    if not [s for s in steps if str(s).strip()]:
        return "steps must be a non-empty list"

    unlabelled = [c["run"] for c in checks if c.get("label") == "unlabelled"]
    if unlabelled:
        return ("every check needs a label. `goal` — it must FAIL now and "
                "pass when you are done, which is how it proves the work "
                "happened. `guard` — it PASSES now and must keep passing. "
                "Unlabelled: " + ", ".join(u[:60] for u in unlabelled))

    from kernel.verifier.artifact_kind import problems as artifact_problems

    bad = []
    for c in checks:
        if c.get("kind") == "artifact":
            for problem in artifact_problems(c.get("spec") or {}):
                bad.append(f"  {c['run']}: {problem}")
    if bad:
        return ("an artifact check is not usable as written.\n"
                + "\n".join(bad)
                + "\n\nAn artifact check is satisfied when you emit_artifact "
                  "something matching it, e.g. {expect: \"artifact\", "
                  "role: \"report\", label: \"goal\"}.")

    # COMMAND CHECKS ONLY: an artifact check's `run` is the notation
    # `artifact[role=report]`, which has nothing to cd into and would be
    # refused for missing a prefix no shell will ever read.
    from kernel.tools.check_paths import refusal_for as path_refusal

    refusal = path_refusal([{"command": c["run"]} for c in checks
                            if c.get("kind", "command") == "command"])
    if refusal:
        return refusal
    return None


def digest(steps, rationale: str, checks) -> str:
    """The content hash an approval binds to.

    The kernel's, not a local one: a plan edited after approval must stop
    matching in exactly the same way here as there, and two hash functions
    for one concept is how they come to disagree about what was approved.
    """
    from kernel.harness.park_approval import plan_hash
    return plan_hash(list(steps), rationale or "", checks or [])


def as_markdown(steps, rationale: str, checks) -> str:
    """The plan a person reads, rendered from the plan a machine checked.

    The markdown used to be the SOURCE -- parsed for a task DAG -- so the
    two could disagree about what had been approved. It is a view now.
    """
    out = ["# Plan", ""]
    if rationale:
        out += ["## Rationale", "", rationale.strip(), ""]
    out += ["## Steps", ""]
    out += [f"{i}. {str(s).strip()}" for i, s in enumerate(steps, 1)]
    if checks:
        out += ["", "## Checks", ""]
        for c in checks:
            label = c.get("label", "unlabelled")
            out.append(f"- `{c.get('run', '')}` — **{label}**")
    return "\n".join(out) + "\n"
