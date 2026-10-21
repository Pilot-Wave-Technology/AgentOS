"""`submit_story_review` — the reviewer's only way out.

THE FINISH TOOL, and the only thing on that surface with an effect. The
review profile holds four read tools, `skill`, `goal_knowledge`,
`artifact_read` and `story_diff`; none of them changes anything. This
one writes the review, moves the story to `reviewed` and ends the
execution, which is why the prompt says to call it once, last.

IT CANNOT REFUSE TO LAND. `done -> reviewed` is gated on the row this
writes, so a review that fails to record leaves its story at `done` for
ever -- holding its assistant thread, its protected workspace and, since
`reclaim` now stands off during the phase, its node. The write, the
transition and the end of the execution are one transaction in
`review_finish.record_review`; delivery to the GO is announced
afterwards and cannot hold any of it up.

`approve` IS NOT ALWAYS AVAILABLE, and this enforces it rather than
trusting the prompt to. A reviewer that skimmed and approved is worse
than no reviewer: it converts "nobody looked" into "somebody looked and
it was fine", which is the one claim this whole design exists to make
true. So a verdict of `approve` alongside unexamined churn is downgraded
to `partial`, the downgrade is recorded IN the review, and the reviewer
is told -- it is not silently accepted and not refused either, because
refusing would cost a leg and the leg budget is three.
"""
from __future__ import annotations

import json
import logging

from kernel.harness.models.types import ToolResult

logger = logging.getLogger(__name__)

VERDICTS = ("approve", "partial", "revise", "code_not_reviewed", "incomplete")

# Long enough that a one-line answer cannot pass as a review; short
# enough that an honest `code_not_reviewed` is not blocked by a word
# count. The failure this guards is a model that calls the finish tool
# to end the leg rather than to report anything.
MIN_SUMMARY = 120


def _render(args: dict, verdict: str, downgraded: str) -> str:
    """The markdown a person reads, from the structured answer."""
    out = [f"# Story review — {verdict}", ""]
    if downgraded:
        out += [f"> **Downgraded from `approve`.** {downgraded}", ""]
    out += [str(args.get("summary") or "").strip(), ""]

    coverage = str(args.get("coverage") or "").strip()
    if coverage:
        out += ["## Coverage", coverage, ""]
    not_examined = [str(x) for x in (args.get("not_examined") or [])]
    if not_examined:
        out += ["## Not examined",
                *(f"- {x}" for x in not_examined), ""]

    findings = args.get("findings") or []
    if findings:
        out += ["## Findings", ""]
        for f in findings:
            if not isinstance(f, dict):
                continue
            where = "/".join(str(f.get(k) or "") for k in ("repo", "file")
                             if f.get(k))
            line = f.get("line")
            where += f":{line}" if line else ""
            sev = str(f.get("severity") or "note")
            out.append(f"- **{sev}** `{where}` — {f.get('claim') or ''}")
            why = str(f.get("why_it_matters") or "").strip()
            if why:
                out.append(f"  - {why}")
        out.append("")

    for key, heading in (("plan_drift", "Plan drift"),
                         ("cross_branch", "Cross-branch"),
                         ("claim_check", "Claim check")):
        body = str(args.get(key) or "").strip()
        if body:
            out += [f"## {heading}", body, ""]
    return "\n".join(out).strip() + "\n"


async def submit_story_review(conn, envelope, tool_args: dict,
                              tool_call_id: str) -> ToolResult:
    """Record the review and end the phase. The reviewer's last call."""
    execution_id = getattr(envelope, "execution_id", None)
    if not execution_id:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error="submit_story_review: no execution on the envelope.",
            exit_code=1)

    verdict = str(tool_args.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"submit_story_review: verdict must be one of "
                  f"{', '.join(VERDICTS)}; got {verdict!r}.",
            exit_code=1)

    summary = str(tool_args.get("summary") or "").strip()
    if len(summary) < MIN_SUMMARY:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"submit_story_review: `summary` is {len(summary)} "
                  f"characters and a review needs at least {MIN_SUMMARY}. "
                  f"Say what you read, what you concluded, and why. If you "
                  f"could not read the diff, say that and use verdict "
                  f"`code_not_reviewed`.",
            exit_code=1)

    # THE DOWNGRADE, not a refusal. Refusing would cost one of three
    # legs and the reviewer would likely repeat itself; recording the
    # correction costs nothing and is more honest than either.
    downgraded = ""
    not_examined = [str(x) for x in (tool_args.get("not_examined") or []) if x]
    if verdict == "approve" and not_examined:
        verdict = "partial"
        downgraded = (
            f"The reviewer answered `approve` while reporting "
            f"{len(not_examined)} unexamined item(s). `approve` means "
            f"everything with material churn was read.")

    text = _render(tool_args, verdict, downgraded)
    payload = {k: tool_args.get(k) for k in
               ("coverage", "not_examined", "findings", "plan_drift",
                "cross_branch", "claim_check")}

    # REPORTED, NOT WRITTEN. This runs in the pod, whose database
    # identity is scoped to the `kernel` schema -- `story_reviews` and
    # `stories` are not its to write. The control panel records it.
    # Threaded because the call is synchronous urllib and the leg loop
    # is one event loop shared by everything else in the process.
    import asyncio

    from kernel.worker.review_finish import report_review

    ok = await asyncio.to_thread(
        report_review, int(execution_id), verdict=verdict,
        review_text=text, reviewer=f"reviewer-{execution_id}")

    if not ok:
        # AN ERROR, BECAUSE IT FAILED. The first version returned
        # exit_code 0 with `handed_off: true`, reasoning that a retry
        # could not succeed and would cost a leg. That reasoning was
        # sound and the result was worse: `exit_code 0` reads to the
        # model as DONE, so it stopped, the leg ended, and the next one
        # began from nothing and repeated the whole review. Three legs,
        # three good reviews, none recorded.
        #
        # Saying it failed is honest, and the run is bounded either way:
        # the ceiling ends the phase after three legs and the deadline
        # sweep records `not_produced` from outside the pod.
        logger.error("submit_story_review: the control panel did not take "
                     "the review for execution %s", execution_id)
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error="submit_story_review: the control panel did not accept "
                  "the review. This is not something you can fix by "
                  "re-reading; if it fails again, stop.",
            exit_code=1)

    logger.info("review submitted for execution %s: %s (%d findings)",
                execution_id, verdict, len(tool_args.get("findings") or []))
    return ToolResult(
        tool_call_id=tool_call_id,
        output=json.dumps({"recorded": True, "verdict": verdict,
                           "downgraded": bool(downgraded)}),
        exit_code=0)
