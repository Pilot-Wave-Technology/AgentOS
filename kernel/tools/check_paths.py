"""A check must say which mount it runs in, or the Verifier guesses.

THE CONVENTION ALREADY EXISTS and `verify.environment_for` documents why:

    One global PATH made the first repository's .venv shadow the second's:
    `cd repos/B && pytest` resolved A's pytest because A sorts first. The
    convention every check follows -- `cd <mount> && ...` -- names the target,
    so the target's executables go FIRST.

What did not exist is anything making an executor follow it. Observed, from a
plan that was approved and ran:

    .venv/bin/pytest repos/Pilot-Wave-Technology__multi_agent_dummy_repo/tests

Two frames of reference in one command. `.venv/bin/pytest` is relative to the
MOUNT — setup puts `.venv/` inside the tree — while `repos/<owner>__<name>/tests`
is relative to the workspace ROOT. Neither cwd makes both halves true. With one
repo it is wrong in a way that usually still works; with two it silently runs
the wrong repository's interpreter, because `environment_for` finds no `cd`,
selects no target, and falls back to alphabetical order.

WHY REFUSE AT REGISTRATION RATHER THAN FIX IT AT VERIFY TIME. The person is
right there, looking at the plan, deciding whether to approve it. A refusal now
costs them one edit. The same problem found at verification costs a leg, a
verdict, and a diagnosis — and the failure reads as "the tests are broken",
which is the one thing it is not. This is the mechanical half of a defect whose
other half is a model writing plausible-looking commands: we cannot stop it
guessing, but we can stop the guess being accepted silently.

DELIBERATELY NARROW. This refuses one thing: a command that NAMES a mount
directory without a `cd` into one. It says nothing about whether the command is
a good check, whether the path exists, or whether the test names are real —
those are judgements, and a validator that makes judgements is one people learn
to route around.
"""

from __future__ import annotations

import re

# `cd <somewhere> &&` at the head of the command — the same shape
# `verify._CD` matches when it resolves which mount a check belongs to.
_LEADS_WITH_CD = re.compile(r"^\s*\(?\s*cd\s+[^\s&|;]+", re.I)

# The layout every multi-repo workspace uses (`mounts.REPO_LAYOUT`).
# A command that mentions one is talking about a path from the workspace root.
_MOUNT_PATH = re.compile(r"(?<![\w/.-])repos/[A-Za-z0-9._-]+__[A-Za-z0-9._-]+")


def offending_checks(checks) -> list[dict]:
    """The checks that name a mount without saying which one to run in.

    Returns one entry per bad check with the command and a fix, so the caller
    can hand the executor something it can act on rather than a verdict.
    """
    out: list[dict] = []
    for raw in checks or []:
        command = (raw.get("command") if isinstance(raw, dict) else raw) or ""
        command = str(command).strip()
        if not command:
            continue
        mounts = _MOUNT_PATH.findall(command)
        if not mounts or _LEADS_WITH_CD.match(command):
            continue
        mount = mounts[0]
        # The same command with the mount prefix stripped, run from inside it.
        inner = re.sub(r"(?<![\w/.-])" + re.escape(mount) + r"/?", "", command)
        inner = re.sub(r"\s{2,}", " ", inner).strip()
        out.append({
            "command": command,
            "mount": mount,
            "suggestion": f"cd {mount} && {inner}" if inner else
                          f"cd {mount} && <command>",
        })
    return out


def refusal_for(checks) -> str:
    """Prose for the executor, or "" when every check names its mount.

    Addressed to the model that wrote them, in the terms it can act on: what
    is wrong, why it matters at verification rather than now, and the exact
    replacement. A refusal a model cannot act on is a loop.
    """
    bad = offending_checks(checks)
    if not bad:
        return ""

    lines = [
        f"{len(bad)} of your checks name a repository directory but do not "
        f"say which one to run in, so the Verifier cannot tell which "
        f"environment they need.",
        "",
        "A check runs with its own cwd. `cd <mount> && <command>` is what "
        "names the target: the Verifier reads that prefix to put THAT repo's "
        "interpreter and tools first on PATH. Without it, the first mount "
        "alphabetically wins — which is silently the wrong repository as soon "
        "as a story touches two, and is why a passing test suite can fail "
        "under verification for no reason visible in the work.",
        "",
    ]
    for b in bad:
        lines += [f"  wrong:  {b['command']}", f"  right:  {b['suggestion']}",
                  ""]
    lines.append("Re-register the plan with the checks written that way.")
    return "\n".join(lines)
