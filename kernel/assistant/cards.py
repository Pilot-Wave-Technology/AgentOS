"""The plan a card is asking about, carried with the card.

WHY THIS EXISTS. `questions.question_text` for a plan approval is the
label `plan approval` -- thirteen characters. The plan itself (steps,
checks, rationale) went into `payload`, and every reader of the card
read the row and not the payload. So the assistant announced *"the
executor is parked on question 197, approve it in the dashboard"* and
could not say what it would be approving; the person then asked the
only sensible question, which was "approve WHAT?".

A card that asks for a decision must carry the thing being decided.
That is the whole of this module, and it is one function so that the
inbox tool, the live notifier and the control panel's chat card cannot
drift into three different answers.

WHAT A PLAN IS. `steps` is the model's own numbered intent. `checks`
are the commands the verifier will run, and they are the honest part of
the card -- a person reading `pytest tests/test_api.py` learns more
about what is about to happen than any prose. `rationale` is the one
line of why. Anything else in the payload is not shown: it is the
harness's business, and padding a decision card with it is how a card
stops being read.
"""

from __future__ import annotations

import json

# A plan is a decision; a long one is still a decision, but a card is
# not a document. These bounds keep an approval readable in a chat
# bubble without ever hiding a step -- the count is always stated.
MAX_STEPS = 24
MAX_CHECKS = 12
MAX_CHARS = 600


def _loads(payload) -> dict:
    """asyncpg hands back jsonb as str on some paths, dict on others."""
    if payload is None:
        return {}
    if isinstance(payload, (str, bytes)):
        try:
            return json.loads(payload) or {}
        except (ValueError, TypeError):
            return {}
    return payload if isinstance(payload, dict) else {}


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def plan_of(payload) -> dict | None:
    """The decision inside an authorization question, or None.

    Returns None rather than an empty shell when there is no plan: a
    card that says `plan: {}` reads as "the plan is empty", which is a
    different and much worse claim than "this question carries no plan".
    """
    data = _loads(payload)
    steps = [_clip(s) for s in (data.get("steps") or []) if str(s).strip()]
    raw_checks = data.get("checks") or []
    checks = []
    for c in raw_checks:
        if isinstance(c, dict):
            run = _clip(c.get("run") or "", 300)
            if run:
                checks.append({"label": str(c.get("label") or ""), "run": run})
        elif str(c).strip():
            checks.append({"label": "", "run": _clip(str(c), 300)})
    rationale = _clip(data.get("rationale") or "")
    if not (steps or checks or rationale):
        return None
    return {
        "rationale": rationale,
        "steps": steps[:MAX_STEPS],
        "checks": checks[:MAX_CHECKS],
        # The counts are of the WHOLE plan, not the shown slice. A card
        # that silently truncates a fifteen-step plan to twelve asks for
        # approval of something the person did not see.
        "step_count": len(steps),
        "check_count": len(checks),
        "truncated": len(steps) > MAX_STEPS or len(checks) > MAX_CHECKS,
    }


# `dryrun_of` WAS HERE AND MUST NOT COME BACK (R1). It rendered a
# "Dry-run" section on every plan-approval card from `questions.dryrun`.
# That column lost its writer when plan 5.6 retired the pre-approval dry
# run and `2e65fcf8` removed the last remnant, so the function was
# handed `None` every time and returned `{"status": "pending"}` — and
# the chat card, which keys the italic line on exactly that, told every
# person approving a plan "Dry-run still running — you can approve now;
# it is advisory." There was no dry run. It was not still running. It
# was never going to finish.
#
# A card that asks for a decision must carry the thing being decided
# (see above); it must not also carry a reassurance about evidence that
# does not exist. Nothing renders it now — `card.dryrun` is simply
# absent, and index.html's `if (dr && ...)` guards handle that.
#
# When E19 lands the warm verification pod, the goal criteria run
# against the BASE COMMIT and there is a real measurement to show. That
# is the moment to write this again, against whatever column E19 fills.


def as_text(plan: dict | None) -> str:
    """The plan as the assistant would say it. Plain text, no markup.

    The assistant is a language model reading a tool result; giving it
    a rendered block it can quote verbatim is more reliable than giving
    it three lists and hoping the formatting survives.
    """
    if not plan:
        return ""
    out = []
    if plan.get("rationale"):
        out.append(plan["rationale"])
    if plan.get("steps"):
        out.append("")
        out.append(f"Steps ({plan['step_count']}):")
        out += [f"  {i}. {s}" for i, s in enumerate(plan["steps"], 1)]
    if plan.get("checks"):
        out.append("")
        out.append(f"Verified by ({plan['check_count']}):")
        for c in plan["checks"]:
            tag = f"[{c['label']}] " if c["label"] else ""
            out.append(f"  - {tag}{c['run']}")
    if plan.get("truncated"):
        out.append("")
        out.append("(card shows the first steps; the full plan is longer)")
    return "\n".join(out)


def story_key(story_id, execution_id):
    """What a card, a status line or a proposal calls the thing it is about.

    ONE STORY ID EVERYWHERE. A story has a real id and that is the only
    name it goes by -- the assistant, the thread, the plan folder and the
    verdict card all say `de5512b2`, never a run number that happens to
    address the same work today. An ERRAND has no story, and then the run
    number is the only name there is.

    The subtlety is what to do in that second case, and three modules
    quietly answered it differently: `write` returned the run number,
    `read` and the notifier returned `str()` of it. A caller that passed
    9680 got "9680" back from one surface and 9680 from another, so the
    obvious `if reply["story"] == story` was false half the time. The
    stringification bought nothing -- nothing renders a run number
    differently for having quotes around it -- and it invented a story id
    that no table contains.
    """
    return story_id if story_id else execution_id
