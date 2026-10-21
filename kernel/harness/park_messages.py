"""Rendering a human's reply so the model reads it as a reply.

SPLIT OUT OF `park.py` (plan 11.5c). The only `park_*` module with no
`conn` and no I/O: pure functions from an answered request row to the
text that goes in front of the next leg's task. That makes it the one
part of parking a test can exercise without a database, which is why the
seam is drawn here rather than folded into `park_read`.

THE WHOLE RISK OF PARKING LIVES IN THIS FILE. A model that has spent
forty steps doing what it is told reads anything in this position as one
more instruction. `_direction_message` argues at length about why; read
it before changing any wording here, and do not compress these strings
on the grounds that they are long. They are long on purpose.
"""

from __future__ import annotations

def _direction_message(request: dict) -> str:
    """What the executor is told after a hold is released.

    THE WHOLE RISK IS HERE. A model that has spent forty steps doing
    what it is told reads anything in this position as one more thing to
    do — which is the same failure that made a question unusable as a
    steer in the first place. So this says three things it cannot get
    wrong: you did not ask this, their reply is not permission, and if
    it changes the work then the work needs a plan.

    The middle case is the one that bites. "Use FastAPI then" reads
    exactly like an instruction, and obeying it directly would put a
    rewritten application in front of somebody whose last sight of the
    plan said Flask.
    """
    p = request.get("payload") or {}
    # TWO WAYS TO BE HELD, and the executor must not be told the wrong one.
    # A hold from the answerer carries the answer that caused it; one a person
    # pressed carries none, because there was no question. Saying "your
    # question was answered" about a button somebody pressed sends the model
    # looking for an exchange that never happened.
    by_person = not (p.get("answer") or "").strip()
    if by_person:
        lines = [
            "## A person paused this run", "",
            "Somebody working with you stopped this run from the story's "
            "conversation. Nothing is wrong and nothing is lost — your "
            "workspace, your branch and your journal are exactly as you left "
            "them, and this is the same park a plan approval takes.", "",
        ]
    else:
        lines = [
            "## Work was paused while a person's question was answered", "",
            "YOU DID NOT ASK THIS. While you were working, somebody asked a "
            "question about this story. It was answered from the record and a "
            "copy of your tree — not by you — and the answer raised something "
            "they had to settle before you went further. That is why the last "
            "leg ended.", "",
        ]
    if p.get("asked"):
        lines += ["WHAT THEY ASKED:", "", f"  {p['asked'].strip()}", ""]
    if p.get("answer"):
        lines += ["THE ANSWER THEY WERE GIVEN:", "", p["answer"].strip(), ""]
    if p.get("decision"):
        lines += ["WHAT THEY WERE ASKED TO DECIDE:", "",
                  f"  {p['decision'].strip()}", ""]
    lines += ["WHAT THEY SAID:", "", (request.get("answer") or "").strip(), ""]
    lines += [
        "HOW TO TREAT IT. This is a decision about direction. It is not a "
        "new instruction, it is not an approval, and it did not change the "
        "scope, the criteria or what done means — an amendment is the only "
        "thing that changes those, and you would see it separately.", "",
        "  - If it changes NOTHING about what you were going to do, carry "
        "on exactly where you left off. Do not redo work, and do not "
        "re-register a plan that already stands.", "",
        "  - If it changes WHAT you will do — a different library, a "
        "different shape, work added, work dropped — that is a new cycle. "
        "Say so with register_plan and wait for a person to approve it. Do "
        "NOT start. The approval you had was for the plan they have just "
        "changed.", "",
        "  - If you cannot tell which of the two it is, treat it as the "
        "second. A plan costs one round trip; changing the work without "
        "one means the first they see of it is a finished diff.",
    ]
    return "\n".join(lines)


def format_answer_for_prompt(request: dict) -> str:
    """Render a human's reply so the model sees it as a reply."""
    if request["kind"] == "direction":
        # Returned whole: "Continue from here" is exactly the wrong
        # closing line for a reply that may mean stop and re-plan.
        return _direction_message(request)
    if request["kind"] == "plan_approval":
        # Step-by-step notes whenever a person gave any. Feedback on a
        # numbered list is about its items, and flattening it into prose
        # leaves the model guessing which of five steps was meant.
        detailed = format_plan_feedback(request)
        if detailed:
            return detailed + "\n\nContinue from here."
        head = "## Your plan was reviewed"
        asked = "\n".join(f"  {i}. {s}" for i, s in
                          enumerate(request["payload"].get("steps", []), 1))
        body = f"You submitted:\n{asked}\n\nThe reviewer said:\n{request['answer']}"
    else:
        head = "## Your question was answered"
        body = (f"You asked:\n  {request['payload'].get('question', '')}\n\n"
                f"The answer:\n{request['answer']}")
    return f"{head}\n\n{body}\n\nContinue from here."


def format_plan_feedback(request: dict) -> str:
    """A person's notes on a plan, rendered against the steps they are about.

    A note that says "split this" is useless detached from the step it
    was written about — and a single free-text box produces exactly
    that, leaving the model to guess which of five steps is meant.
    """
    steps = (request.get("payload") or {}).get("steps") or []
    notes = {int(n["step"]): n.get("note", "")
             for n in (request.get("step_notes") or [])
             if str(n.get("step", "")).isdigit() and n.get("note")}
    verdict = request.get("verdict") or (
        "declined" if request.get("declined") else "approve")

    if verdict == "approve" and not notes:
        return ""
    head = {"revise": "## Your plan was sent back for revision",
            "declined": "## Your plan was declined"}.get(
                verdict, "## Your plan was approved, with notes")
    lines = [head, ""]
    for i, step in enumerate(steps, 1):
        lines.append(f"  {i}. {step}")
        if i in notes:
            lines.append(f"     ^ {notes[i]}")
    if request.get("answer"):
        lines += ["", request["answer"]]
    if verdict == "revise":
        lines += ["", "The approach is right and this plan is not. Register "
                      "a revised plan that addresses the notes above — do "
                      "not abandon the approach."]
    elif verdict == "declined":
        lines += ["", "This approach was rejected. Do not register a "
                      "variation of it: say what you would do instead, or "
                      "ask."]
    return "\n".join(lines)

