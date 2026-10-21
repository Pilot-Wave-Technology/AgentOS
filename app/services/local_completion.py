"""What happens when a locally-run story is marked done.

A local story has no `kernel.executions` row -- the conductor is never
involved -- so none of the kernel's completion machinery fires for it. Three
things that a cloud story gets for free have to be done here instead.

1. IT MUST REACH `reviewed`, NOT SIT AT `done`.

   The council stands down for every `done` review whenever
   STORY_REVIEW_IN_POD is on, and there is no execution to run an in-pod
   review, so nothing ever called `mark_story_reviewed`. A local story sat at
   `done` for ever.

   That is not merely untidy. `done` leaves `stories.active_thread_id` bound
   -- only `reviewed` and `cancelled` clear it -- which is the stale-binding
   leak that has had to be fixed once already, and it keeps the story's
   workspace on the SHCA protect list permanently.

   Nor was anything complaining: the review-deadline backstop requires
   `EXISTS (kernel.executions)`, so a local story is invisible to it. Silence
   by accident, not by decision.

   So: a fourth `skipped_*` verdict beside `skipped_errand`, `skipped_merge`
   and `skipped_diff`, recorded through the same writer. The reason goes on
   the row rather than being inferred from the absence of text -- "a story
   that reached `reviewed` without a review and one that earned it are
   different facts".

2. IT MUST SAY WHAT HAPPENED.

   `stories.result_summary` was never written locally, so the GO's completion
   message rendered `Result: No result summary`. Composed here from what the
   run actually left behind, and written with the same guarded UPDATE
   `errand_result.record` uses, so a human's or the assistant's own summary
   always wins.

3. THE GO'S MESSAGE MUST NOT BE EMPTY.

   Two lookups in the legacy completion message are dead for a local story --
   it joins `threads WHERE task_id = story_id` (a local thread has no
   `task_id`) and filters `role = 'assistant'` (local turns are written as
   `'agent'`). Both are fixed in `go_events`; this module only makes sure
   there is something for them to find.
"""

import logging

logger = logging.getLogger(__name__)

# The fourth `skipped_*`. Named rather than reusing one of the others,
# because "nobody reviewed this because it ran on a laptop" is a different
# fact from "nobody reviewed this because it is an errand", and the whole
# point of recording the reason is that the two can be told apart later.
SKIPPED_LOCAL = "skipped_local"


async def compose_result_summary(pool, story_id: str) -> str:
    """What this story left behind, in a few lines.

    FROM WHAT IS ON THE RECORD, not from a model. The approved plan says
    what was meant to happen and the diff says what did; between them a
    person reading the board learns more than a generated paragraph would
    tell them.
    """
    plan = await pool.fetchrow(
        """SELECT plan_json, plan_doc_path FROM plan_versions
            WHERE story_id = $1 AND status IN ('approved', 'executed')
         ORDER BY version DESC LIMIT 1""", story_id)
    approval = await pool.fetchrow(
        """SELECT payload FROM approvals
            WHERE story_id = $1 AND kind = 'diff'
         ORDER BY proposed_at DESC LIMIT 1""", story_id)

    lines = ["Worked locally."]
    if plan and plan["plan_doc_path"]:
        lines.append(f"Plan: {plan['plan_doc_path']}")
    if approval and approval["payload"]:
        import json
        try:
            payload = (approval["payload"] if isinstance(approval["payload"], dict)
                       else json.loads(approval["payload"]))
        except Exception:                                   # noqa: BLE001
            payload = {}
        stat = (payload.get("stat") or "").strip()
        sha = (payload.get("commit_sha") or "")[:12]
        if stat:
            lines.append("Changes: " + stat.splitlines()[-1].strip())
        if sha:
            lines.append(f"Commit: {sha}")
    return "\n".join(lines) if len(lines) > 1 else ""


async def record_result(pool, story_id: str) -> str:
    """Write `result_summary`, unless somebody wrote a better one.

    The guard is in the statement rather than a read-then-write, because
    the assistant's own report path writes the same column and a check
    followed by an update is a race with it.
    """
    text = await compose_result_summary(pool, story_id)
    if not text:
        return ""
    written = await pool.fetchval(
        """
        UPDATE stories SET result_summary = $2, updated_at = now()
         WHERE id = $1 AND COALESCE(btrim(result_summary), '') = ''
        RETURNING id
        """, story_id, text)
    if written:
        logger.info("local_completion: wrote result_summary for %s", story_id)
        return text
    return ""


async def mark_reviewed_as_local(pool, story_id: str, goal_id: str) -> bool:
    """Take the story from `done` to `reviewed`, recording why nobody read it.

    Through `record_review_for_story`, which is the one writer -- a second
    one for this lane would break the same rule the local thread resolver
    broke when it grew its own.
    """
    from kernel.worker.review_finish import record_review_for_story

    # A CONNECTION, NOT THE POOL. `record_review_for_story` wraps its write in
    # `async with conn.transaction()`, and `asyncpg.Pool` has no
    # `.transaction()` -- only a Connection does. Passing the pool raised
    # AttributeError, the best-effort guard in `finish_local_story` swallowed
    # it, and the story stayed at `done` for ever: no `story_reviews` row, the
    # thread still bound, the workspace still on the protect list.
    #
    # Silent by construction, and it was: story 81ee97ea finished with its
    # result summary written and zero review rows. The pod lane never hit this
    # because the worker passes a real connection.
    async with pool.acquire() as conn:
        return await record_review_for_story(
            conn, story_id, goal_id,
            verdict=SKIPPED_LOCAL,
            review_text=(
                "Worked in the author's own editor. A local story is reviewed "
                "by the person running it, as they go — there is no separate "
                "pass, so none is recorded."),
            reviewer="local",
            run_id="local")


async def finish_local_story(pool, story_id: str, goal_id: str) -> dict:
    """Everything a local story needs after its `done` transition.

    BEST-EFFORT, INDIVIDUALLY. A failure to summarise must not stop the
    story reaching `reviewed`, and a failure to reach `reviewed` must not
    lose the summary. Each is logged and the other still runs -- the
    alternative is one bad row deciding that neither happened, which is the
    shape of the sweep failure the card loop was rewritten to avoid.
    """
    out = {"result_summary": "", "reviewed": False, "owes_review": False}

    # A STORY THAT OWES A REVIEW MUST NOT TAKE THE EXEMPTION.
    #
    # `mark_reviewed_as_local` writes verdict `skipped_local` -- "a local
    # story is reviewed by the person running it, as they go". That was true
    # when there was no local review. Now a coding story is reviewed in the
    # session before it reaches `reviewed`, and recording the exemption here
    # would both pre-empt that verdict and move the story out of `done`
    # before anybody had read anything.
    #
    # Errands, merge stories and the engine's own stories still take it, the
    # same three kinds the pod's `owes_a_review` excludes -- and so does any
    # story whose local review profile is missing, which fails closed loudly
    # rather than holding the story open for a review nothing can produce.
    from app.services.local_review import owes_a_review

    try:
        out["owes_review"] = await owes_a_review(pool, story_id)
    except Exception:                                       # noqa: BLE001
        logger.exception("local_completion: review check failed for %s", story_id)
    try:
        # SPEND THE PLAN. The story is finished with, so its approved plan is
        # too -- and if the story is ever reopened, the work that follows is
        # new intent and needs its own plan. The other place a plan is spent is
        # `request_more_work`; a landed diff deliberately does NOT spend one,
        # because a person lands a safe subset mid-plan all the time.
        from app.services.local_cycles import spend_cycle  # noqa: PLC0415
        await spend_cycle(pool, story_id, "story_done")
    except Exception:                                       # noqa: BLE001
        logger.exception("local_completion: spending the plan failed for %s", story_id)
    try:
        out["result_summary"] = await record_result(pool, story_id)
    except Exception:                                       # noqa: BLE001
        logger.exception("local_completion: result summary failed for %s", story_id)
    if not out["owes_review"]:
        try:
            out["reviewed"] = await mark_reviewed_as_local(pool, story_id, goal_id)
        except Exception:                                   # noqa: BLE001
            logger.exception(
                "local_completion: review exemption failed for %s", story_id)
    return out
