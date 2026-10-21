"""A refusal, written for the person who has to do something about it.

WHAT A REFUSAL CARD USED TO SAY. The explainer's paragraph, or — when
the model was unavailable — "A check said no; the executor continues
and will try again." Neither told you WHICH check, how many, what it
printed, or whether the problem was the code at all. Watching a real
refusal, the honest summary of the card was: something failed, we
think it is about customer_ref.

That mattered more than it looks. The one refusal we read closely was
caused by a missing input file, not by the rule the explainer named —
and the card gave a person no way to notice, because it showed a
conclusion and none of the evidence the conclusion came from.

SO: THE NUMBERS FIRST, THEN THE EVIDENCE, THEN THE OPINION. How many
checks failed of how many. Which commands. The tail of what each
printed, which is where a test runner says what it actually found. The
explainer's reading last, marked as a reading — it is a model's
inference over the same lines, and putting it first invites everyone
to stop there.
"""

from __future__ import annotations

import json

# A failing test prints its story at the END: the summary line, the
# assertion, the count. The head is banners and plugin lists.
TAIL = 700

# Enough failures to see a pattern. Beyond this the pattern IS the
# message, and thirty stack traces in a chat card help nobody.
MAX_SHOWN = 4


def _entries(refusal) -> list:
    if isinstance(refusal, list):
        return refusal
    try:
        got = json.loads(refusal or "[]")
    except (ValueError, TypeError):
        return []
    return got if isinstance(got, list) else []


def _short(command: str) -> str:
    """A command a person can recognise, not a path they must parse."""
    cmd = " ".join((command or "").split())
    if len(cmd) <= 120:
        return cmd
    # Keep the ends: the runner is at the front, the arguments that
    # narrow it are at the back, and the middle is repo path.
    return cmd[:70] + " … " + cmd[-45:]


def summarise(refusal, passed_count: int = 0) -> str:
    """The body of a refusal card, as markdown."""
    entries = _entries(refusal)
    failures = [e for e in entries
                if isinstance(e, dict) and (e.get("output")
                                            or e.get("code"))]
    explain = next((e.get("explain") for e in entries
                    if isinstance(e, dict) and e.get("explain")), "")

    if not failures and not explain:
        return ("Verification refused, and the verdict carried no detail. "
                "That is itself worth reporting: a refusal with no "
                "evidence cannot be acted on.")

    total = len(failures) + max(passed_count, 0)
    head = (f"**{len(failures)} of {total} checks failed.**"
            if total else f"**{len(failures)} check(s) failed.**")
    out = [head, ""]

    for e in failures[:MAX_SHOWN]:
        cmd = _short(e.get("locator") or "")
        code = e.get("code") or ""
        exit_code = e.get("exit_code")
        line = f"**`{cmd}`**" if cmd else f"**{code or 'a check'}**"
        if exit_code not in (None, ""):
            line += f"  — exit {exit_code}"
        out.append(line)
        body = (e.get("output") or e.get("reason") or "").rstrip()
        if body:
            out += ["", "```", body[-TAIL:], "```", ""]
        else:
            out += ["", "_(no output — the check failed without saying "
                    "why, which usually means it could not run)_", ""]

    if len(failures) > MAX_SHOWN:
        out.append(f"_…and {len(failures) - MAX_SHOWN} more._")
        out.append("")

    if explain:
        # LAST, AND LABELLED. This is a model reading the same output
        # above. Put first, it becomes the thing everyone quotes, and
        # its confidence does not vary with how right it is.
        out += ["---", "", "**The verifier's reading** (an inference "
                "from the output above, not a measurement):", "",
                explain.strip()]
    return "\n".join(out).strip()


def headline(refusal, passed_count: int = 0) -> str:
    """One line, for a notification or a log."""
    failures = [e for e in _entries(refusal)
                if isinstance(e, dict) and (e.get("output") or e.get("code"))]
    if not failures:
        return "verification refused"
    first = _short(failures[0].get("locator") or "") or "a check"
    more = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
    return f"{first} failed{more}"
