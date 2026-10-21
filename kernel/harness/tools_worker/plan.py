"""`register_plan` — an approach a human approves before work starts.

The same shape as `ask_human` and for the same reasons: it writes
to the database, it parks the run, and it changes nothing outside
the workspace, so it is `contained`.

What it adds is the gate. A profile with `requires_plan` may not
change anything until a plan is approved, so this is the tool that
unblocks the rest of the surface.
"""
from __future__ import annotations

import logging

from kernel.harness.models.types import ToolResult

logger = logging.getLogger(__name__)


async def register_plan(conn, execution_id: int, generation: int,
                        tool_args: dict, tool_call_id: str) -> ToolResult:
    """E6h — submit a plan for approval. Work cannot start until it is approved.

    The hash binds the approval to exactly this plan: `approvals` is
    first-verdict-wins on a content hash, so a plan edited after approval
    no longer matches and cannot inherit the decision.
    """
    from kernel.harness.park import ParkRequested, open_request, plan_hash

    steps = tool_args.get("steps") or []
    rationale = (tool_args.get("rationale") or "").strip()
    if not steps or not isinstance(steps, list):
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="register_plan: steps must be a non-empty list",
                          exit_code=1)

    # F11 — the checks travel WITH the plan, in one act and one park.
    #
    # Not a separate `propose_criteria`, which would permit a plan with
    # no checks: that is the situation being fixed. The person who left
    # the box empty was not careless, they were being asked to write
    # shell commands against a tree they cannot see. You can — you are
    # standing in it.
    from kernel.worker.contracts import normalise

    checks = normalise(tool_args.get("checks"))
    unlabelled = [c["run"] for c in checks if c["label"] == "unlabelled"]
    if unlabelled:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=("register_plan: every check needs a label. `goal` — it "
                   "must FAIL now and pass when you are done, which is how "
                   "it proves the work happened. `guard` — it PASSES now "
                   "and must keep passing. Unlabelled: "
                   + ", ".join(u[:60] for u in unlabelled)),
            exit_code=1)

    # Ec2d — AN ARTIFACT CHECK MUST BE ABLE TO FAIL. Refused here, at the
    # moment somebody can still fix it, because the alternative is the
    # whole errand running and then being turned down by a check that was
    # broken when it was written — a leg spent on nothing, and a refusal
    # about the check rather than the work.
    from kernel.verifier.artifact_kind import problems as _artifact_problems

    bad_specs = []
    for c in checks:
        if c.get("kind") == "artifact":
            for problem in _artifact_problems(c.get("spec") or {}):
                bad_specs.append(f"  {c['run']}: {problem}")
    if bad_specs:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=("register_plan: an artifact check is not usable as "
                   "written.\n" + "\n".join(bad_specs)
                   + "\n\nAn artifact check is satisfied when you "
                     "emit_artifact something matching it, e.g. "
                     "{expect: \"artifact\", role: \"report\", "
                     "label: \"goal\"}."),
            exit_code=1)

    # AND THEY MUST SAY WHERE THEY RUN. A check that names a repository
    # directory without a `cd` into one leaves the Verifier to guess which
    # mount's environment it needs, and it guesses alphabetically — silently
    # the wrong repository the moment a story touches two. Refused HERE, with
    # the person looking at the plan, because the same fault found at
    # verification costs a leg and a verdict and reads as "the tests are
    # broken", which is the one thing it is not.
    from kernel.tools.check_paths import refusal_for as _bad_paths

    # COMMAND CHECKS ONLY. `_bad_paths` looks for a check that names a
    # repository directory without a `cd <mount> &&` prefix, which is a
    # statement about a shell line. An artifact check's `run` is the
    # notation `artifact[role=report]` — nothing to cd into, and a name
    # containing a path-like string would otherwise be refused for missing
    # a prefix no shell will ever read.
    path_refusal = _bad_paths([{"command": c["run"]} for c in checks
                               if c.get("kind", "command") == "command"])
    if path_refusal:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="register_plan: " + path_refusal, exit_code=1)

    # Stage L — the decisions this plan is MAKING, named individually.
    #
    # `rationale` is one paragraph for the whole plan, and a plan is a
    # bundle: cycle 2 may keep the framework choice and change only the
    # storage one. Without addressable entries the only supersession
    # available is "everything in cycle 1 is stale", which is false and
    # makes a planner work around a decision that still stands.
    #
    # Written HERE because this is the moment of maximum knowledge — the
    # executor is composing the plan anyway — and because the entries then
    # ride the same hash and the same human approval as the rest of it. A
    # decision read back off an approved plan is `observed`; the same fact
    # summarised afterwards by anyone would be `unproven`.
    #
    # Optional, so an executor that omits them still registers a valid
    # plan. `payload` is jsonb, so this costs no schema change at all.
    from kernel.knowledge.decisions import normalise as normalise_decisions

    decisions = normalise_decisions(tool_args.get("decisions"))

    payload = {"steps": [str(s) for s in steps], "rationale": rationale,
               "checks": checks}
    if decisions:
        payload["decisions"] = decisions

    # WHAT CHANGED ABOUT THE DEFINITION OF DONE.
    #
    # Goal checks ARE what done means, and a re-plan can quietly narrow
    # them. On KER25 the first card carried three goal checks exercising
    # the HTTP layer -- `/add` with "inf" -> 422, `/multiply` overflow ->
    # 400, `/add` with true -> 422. The second, ten minutes later,
    # carried two schema-only checks; the overflow check was gone and
    # nothing tested the endpoint at all. Both were approved. Seeing it
    # required diffing two cards nobody had reason to compare.
    #
    # Not a refusal: narrowing can be right, and the executor that did it
    # went on to do the stronger work anyway. But an approver should not
    # have to reconstruct the delta to notice there is one.
    try:
        changed = await _check_delta(conn, execution_id, checks)
        if changed:
            payload["checks_changed"] = changed
    except Exception:                                       # noqa: BLE001
        logger.exception("check-surface delta failed; the plan stands")
    # The digest deliberately covers steps + rationale + checks only.
    # Decisions restate what those already say, and folding them in would
    # mean a plan re-registered with better-worded decisions no longer
    # matches an approval a person already gave.
    digest = plan_hash(payload["steps"], rationale, checks)
    try:
        request_id = await open_request(
            conn, execution_id, generation, "plan_approval", payload, digest)
    except Exception as e:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"register_plan: this run is already waiting on a human ({e})",
            exit_code=1)

    if not isinstance(request_id, int):
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"register_plan refused [{request_id.code}]: "
                  f"{request_id.message}",
            exit_code=1)

    # The plan, mirrored into every repo it is about to change --
    # agentos_plans/<story>/plan-NN.md -- so it rides the story's own
    # commits and shows in the PR the human merges. Registered first,
    # mirrored second: a workspace that cannot be written must not
    # block an approval on a courtesy copy.
    try:
        from kernel.harness.planfiles import persist_plan
        written = await persist_plan(conn, execution_id, payload)
        if written:
            logger.info("execution %s: plan mirrored to %s",
                        execution_id, ", ".join(written))
    except Exception:                                       # noqa: BLE001
        logger.exception("plan file mirror failed; the registration stands")

    # NO DRY RUN IS PROMISED, BECAUSE NONE HAPPENS. This line used to say
    # the checks "are being run against the current tree to confirm they
    # can tell the difference" -- a claim that was true while F11's dry
    # run existed and a lie from the moment it was retired. A person
    # approving a plan was being told the checks had been exercised; they
    # had not. Say what is actually known: the checks are declared, and
    # nobody has run them yet.
    raise ParkRequested(
        request_id, "plan_approval",
        f"parked for approval of a {len(steps)}-step plan"
        + (f" and {len(checks)} declared check(s), which have NOT been run "
           f"yet — they define done, and the first time they run is when "
           f"the work claims to be finished"
           if checks else " with NO checks — nothing will define done"))


async def _check_delta(conn, execution_id: int, checks: list) -> dict | None:
    """How this check surface differs from the last one proposed.

    Returns `{"removed": [...], "added": [...], "goal_count": [was, now]}`
    or None when there is no previous plan, or nothing moved.

    Compares against the last PLAN, not the last approved contract: the
    question is what the approver saw a moment ago, which is what they
    will assume still holds.
    """
    import json as _json

    row = await conn.fetchrow(
        """
        SELECT payload FROM questions
         WHERE execution_id = $1 AND kind = 'authorization'
           AND question_text = 'plan approval'
         ORDER BY id DESC LIMIT 1
        """, execution_id)
    if row is None or not row["payload"]:
        return None
    prev = row["payload"]
    if isinstance(prev, str):
        prev = _json.loads(prev)
    before = prev.get("checks") or []
    if not before:
        return None

    def runs(cs, label=None):
        return {str(c.get("run") or "") for c in cs
                if label is None or c.get("label") == label}

    removed = sorted(runs(before) - runs(checks))
    added = sorted(runs(checks) - runs(before))
    was, now = len(runs(before, "goal")), len(runs(checks, "goal"))
    if not removed and not added and was == now:
        return None
    out = {"removed": removed, "added": added, "goal_count": [was, now]}
    if now < was:
        # NAMED, because this is the direction that matters. A surface
        # with fewer goal checks than the one just approved is a weaker
        # definition of done, and the card should say so in words rather
        # than leave it to be inferred from two lists.
        out["note"] = (f"this plan has {now} goal check(s); the previous "
                       f"had {was}. Fewer goal checks means less has to "
                       f"be true for this story to count as done.")
    return out
