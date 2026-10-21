"""The two documents the ledger produces.

`open_findings_doc` is what every stage opens on, and it is the whole
dedup mechanism: it hands the reviewer the keys, so reusing one is
recognition rather than recall. Without it the reviewer would have to
invent a key for something it has never seen named, and two cycles would
never agree.

`packet` is the floor under "every goal review produces its findings".
When the convener runs, its synthesis is the packet. When the convener
fails -- or never starts, because the pod died or the deadline swept the
run -- this draws one from the ledger in code. It is plainer than a
synthesised packet and it is never absent, which is the property that
matters: a review that produced nothing teaches the GO that reviews can
be ignored.
"""

from __future__ import annotations

# A packet the GO must act on is not a reading list. Five is the cap the
# skill enforces on the GO's side too, and matching them means the GO is
# never asked to triage more than it may answer.
MAX_ACTIONS = 5

# The point at which "still open" becomes "nobody is dealing with this".
# Three twelve-hour cycles is a day and a half -- long enough that a
# genuinely-in-progress fix is not nagged, short enough that a dropped
# one surfaces while it still matters.
ESCALATE_AFTER = 3


def _line(f: dict) -> str:
    where = f"story:{f['story_id']}" if f.get("story_id") else "goal"
    return f"- [{f['key']}] ({f['severity']}, {where}) {f['title']}"


def open_findings_doc(findings: list[dict]) -> str:
    """`OPEN_FINDINGS.md` — the keys, and what the GO did about them."""
    if not findings:
        return (
            "# Open findings\n\n"
            "_(none — this goal has no open findings. Anything you raise "
            "is new, so give it a fresh kebab-case key.)_\n")

    out = [
        "# Open findings", "",
        "These are already open. **If what you find is one of these, reuse "
        "its key** — do not invent a new one for the same thing, or it will "
        "be reported to the orchestrator twice under two names.", "",
        "You must account for **every one of these in your scope** under a "
        "`## Previously open` heading: `still open`, `resolved`, or `no "
        "longer applies`. One you do not mention stays open — silence reads "
        "as *you did not look*, which is not the same as *it is fixed*.", "",
    ]
    for f in findings:
        out.append(_line(f))
        seen = f.get("times_seen") or 1
        age = f"seen {seen} cycle{'s' if seen != 1 else ''}"
        out.append(f"  - {age}, raised by {f.get('raised_by') or 'a reviewer'}")
        if f.get("body"):
            out.append(f"  - {f['body']}")
        if f.get("go_said"):
            out.append(f"  - **the orchestrator answered:** {f['go_said']}")
    out.append("")
    return "\n".join(out)


def _bucket(findings: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """New this cycle, escalating, and the quiet remainder."""
    new, escalating, rest = [], [], []
    for f in findings:
        seen = f.get("times_seen") or 1
        if seen == 1:
            new.append(f)
        elif seen >= ESCALATE_AFTER or f.get("go_said"):
            # A GO response with the finding still open means the answer
            # did not land. That is worth escalating at any age.
            escalating.append(f)
        else:
            rest.append(f)
    return new, escalating, rest


def packet(goal_id: str, findings: list[dict], failures: list[str]) -> str:
    """The fallback packet, drawn from the ledger with no model involved."""
    new, escalating, rest = _bucket(findings)
    out = [f"# Goal review — {goal_id}", ""]

    if failures:
        # SAID PLAINLY, BECAUSE COVERAGE IS PART OF THE FINDING. A packet
        # that omits which reviewers failed reads as a full review that
        # happened to be quiet, and quiet is the wrong conclusion.
        out += [
            "> **This packet was assembled by the harness, not by the "
            "convener.** The following stages did not complete, so this "
            "review is incomplete: " + ", ".join(failures) + ". "
            "The findings below are what the stages that did run recorded.",
            ""]

    if not findings:
        out += [
            "## State of the goal", "",
            "No findings were recorded this cycle."
            + (" Every stage failed, so this is not a statement that the "
               "goal is healthy — nothing successfully looked at it."
               if failures else
               " The reviewers ran and raised nothing new."), ""]
        return "\n".join(out)

    if new:
        out += ["## New this cycle", ""] + [_line(f) for f in new] + [""]
    if escalating:
        out += ["## Escalating", ""]
        for f in escalating:
            out.append(_line(f))
            out.append(f"  - open {f.get('times_seen', 1)} cycles")
            if f.get("go_said"):
                out.append(f"  - previously answered: {f['go_said']}")
        out.append("")
    if rest:
        out += ["## Still open", ""] + [_line(f) for f in rest] + [""]

    out += [
        "## Recommended actions for GO", "",
        f"Pick at most {MAX_ACTIONS}, worst first. Anything under "
        f"**Escalating** has already been raised and not dealt with — "
        f"prefer those over new items.", ""]
    return "\n".join(out)
