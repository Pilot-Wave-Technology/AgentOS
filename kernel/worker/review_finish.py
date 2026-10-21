"""Recording a story review, and ending the phase that produced it.

ONE FUNCTION WRITES THE REVIEW, whichever path produced it: the reviewer
calling its finish tool, the harness synthesising one because the
reviewer never did, or the deadline sweep doing it from outside the pod.
They differ only in what the review SAYS.

TWO HALVES, BECAUSE NOT EVERY PROCESS MAY WRITE THIS. `record_review`
does the work and runs where the privilege is -- the control panel and
the conductor connect as a role holding INSERT on `story_reviews` and
UPDATE on `stories`. `report_review` is what a POD calls: its DSN is
assembled in-container from an `execution-db` Secret, a scoped execution
identity with nothing outside the `kernel` schema, so it posts to
`/internal/story-review` and the panel records.

THAT SCOPING IS DELIBERATE AND WORTH KEEPING. The pod's sandbox runs
model-authored code and shares its network namespace; `UPDATE` on
`stories` is not a privilege to hand it so that a convenience can stay
convenient. It is the same line that keeps the bucket credential out --
see `/internal/artifact`, which exists for exactly this reason one layer
over.

An earlier version of this file argued the opposite: that the transition
must not sit behind a network hop, so the pod should write directly.
That was right about the stakes and wrong about the facts -- the pod
could not make the write at all, and every submission died on
`permission denied`. What makes the hop acceptable is that its failure
is already covered: `review_deadline` sweeps `done` stories with no
review and records `not_produced` from outside the pod.

SCHEMA-QUALIFIED, AND THAT IS NOT TIDINESS. This runs on two kinds of
connection now. The kernel's carry `search_path = kernel,public`; the
control panel's pooled ones do not, so an unqualified `executions`
resolves in one process and raises `UndefinedTableError` in the other.
Measured: execution 246's reviewer reported its verdict three times and
every POST answered 500 on `relation "executions" does not exist`.
`kernel_decide` solves the same problem by opening a dedicated
connection with the search_path set; qualifying the names is the version
that cannot be forgotten by the next caller.

DELIVERY IS ANNOUNCED, NOT CALLED. The review reaches the GO as an
orchestrator card and a `[REVIEW:story]` wake, both control-panel code,
and the kernel may not import from `app/`. So the row lands and a NOTIFY
follows. A missed NOTIFY costs a card, not a story.

THE STORY IS ALREADY `done` WHEN THIS RUNS. Acceptance moved it there
and put the execution into the review phase; this is the second half.
`mark_reviewed` is guarded on `state = 'done'` for the same reason
`mark_story_reviewed` is: a story that moved while the reviewer was
reading should not be dragged into `reviewed` from somewhere else.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)

# The state a final review covers. Interim reviews of an `in_progress`
# or `blocked` story are a different thing, produced by the goal
# council, and they key on their own `reviewed_state`.
FINAL_STATE = "done"

# Announced after the write lands. `app/` listens and delivers the
# orchestrator card and the GO wake; nothing here waits for it.
READY_CHANNEL = "story_review_ready"

# The control panel route the pod reports through. Named
# alongside the other two halves of the same seam in
# `kernel/workspace/broker.py`.
STORY_REVIEW = "story-review"


async def record_review_for_story(conn, story_id: str, goal_id: str, *,
                                  verdict: str, review_text: str,
                                  reviewer: str, run_id: str,
                                  execution_id: int | None = None) -> bool:
    """Write the review and move the story. The one writer.

    KEYED ON THE STORY, not on an execution, because a story can reach
    `reviewed` without ever having had one. A locally-run story has no
    `kernel.executions` row at all -- the conductor is not involved -- and
    the execution-keyed form could not express it, so a local story sat at
    `done` for ever: the council stands down whenever STORY_REVIEW_IN_POD
    is on, and there was no pod review to take its place.

    `execution_id` is optional and only ends that execution. A story-keyed
    call passes none and nothing is ended, because nothing was running.

    Returns False when there is nothing to record against, which is not an
    error: a review of a story that has been cancelled has nowhere to go
    and nothing to strand.
    """
    if not story_id:
        logger.warning("review: no story to review (run %s)", run_id)
        return False

    async with conn.transaction():
        await conn.execute(
            """INSERT INTO story_reviews
                   (id, story_id, goal_id, reviewed_state, review_text,
                    reviewer_agent_id, run_id, verdict)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (story_id, reviewed_state) DO UPDATE
                   SET review_text = EXCLUDED.review_text,
                       reviewer_agent_id = EXCLUDED.reviewer_agent_id,
                       verdict = EXCLUDED.verdict,
                       reviewed_at = NOW()""",
            uuid.uuid4().hex, story_id, goal_id, FINAL_STATE, review_text,
            reviewer, run_id, verdict)

        # GUARDED ON `done`, and silent when it does not move. A story
        # cancelled while the reviewer was reading is not an error here:
        # the review is still worth keeping, and dragging a cancelled
        # story into `reviewed` would be worse than leaving it.
        #
        # `active_thread_id = NULL` IS PART OF THE SAME WRITE, and is why
        # reaching `reviewed` matters for a local story even when nobody
        # reviewed it: `done` alone leaves the thread bound and the
        # workspace on the protect list for ever.
        moved = await conn.execute(
            "UPDATE stories SET state = 'reviewed', last_reviewed_state = $2, "
            "active_thread_id = NULL, is_active_focus = false, "
            "updated_at = now() WHERE id = $1 AND state = 'done'",
            story_id, FINAL_STATE)

        # THE PHASE IS OVER EITHER WAY. Leaving the execution RUNNING
        # would hold the pod, the admission slot and the node, and
        # `reclaim` is now told to keep its hands off a review phase --
        # so this is the only thing that releases them. A story-keyed
        # call has no execution and skips it.
        if execution_id is not None:
            await conn.execute(
                "UPDATE kernel.executions SET state = 'DONE', phase = NULL, "
                "updated_at = now() WHERE id = $1 "
                "AND state NOT IN ('CANCELLED', 'FAILED')", execution_id)

    logger.info("review: story %s recorded %s (%s)",
                story_id, verdict, moved)
    try:
        await conn.execute(
            "SELECT pg_notify($1, $2)", READY_CHANNEL,
            json.dumps({"goal": goal_id, "story": story_id,
                        "execution": execution_id, "verdict": verdict}))
    except Exception:                                       # noqa: BLE001
        # A CARD, NOT A STORY. The transition above is committed; this
        # only wakes the delivery. The panel's own sweep finds a review
        # nobody delivered.
        logger.exception("review: could not announce %s", story_id)
    return True


async def record_review(conn, execution_id: int, *, verdict: str,
                        review_text: str, payload: dict | None = None,
                        reviewer: str = "") -> bool:
    """Record a review of an EXECUTION. Resolves the story, then delegates.

    A thin wrapper over `record_review_for_story` so that ONE FUNCTION
    WRITES THE REVIEW -- this module's own rule, which a second writer for
    the local lane would have broken in exactly the way the local thread
    resolver broke its.
    """
    row = await conn.fetchrow(
        "SELECT story_id, goal_id, state FROM kernel.executions "
        "WHERE id = $1",
        execution_id)
    if row is None or not row["story_id"]:
        logger.warning("review: execution %s has no story to review",
                       execution_id)
        return False
    return await record_review_for_story(
        conn, row["story_id"], row["goal_id"],
        verdict=verdict, review_text=review_text,
        reviewer=reviewer or f"exec-{execution_id}",
        run_id=f"exec:{execution_id}", execution_id=execution_id)


def report_review(execution_id: int, *, verdict: str,
                  review_text: str, reviewer: str = "") -> bool:
    """Send a review from a POD, which may not write the tables itself.

    `record_review` above is the privileged half and runs where the
    privilege is: the control panel and the conductor both connect as a
    role holding INSERT on `story_reviews` and UPDATE on `stories`. The
    POD does not. Its DSN is built in-container from an `execution-db`
    Secret -- a scoped execution identity -- and that scoping is
    deliberate: the pod's sandbox runs model-authored code and shares
    its network namespace, so `UPDATE` on `stories` is not a privilege
    to grant it for convenience.

    Measured before this existed: execution 245's reviewer wrote three
    good reviews and every one died on `permission denied for table
    story_reviews`, then crashed the leg on the way out, which turned a
    bounded three-leg phase into a loop.

    SYNCHRONOUS AND urllib, for the reasons `emit._put_over_http` gives:
    called through a thread, and the pod's image carries no cloud SDK.
    Returns False rather than raising -- every caller has something
    better to do than die, and `review_deadline`'s sweep is the backstop
    for a review that never lands.
    """
    import json
    import urllib.error
    import urllib.request

    from kernel.workspace import broker, podfs_auth

    url = broker.endpoint(STORY_REVIEW, "STORY_REVIEW_URL")
    token = (os.environ[podfs_auth.ENV_VAR]
             if podfs_auth.ENV_VAR in os.environ else "")
    if not url or not token:
        logger.error("review: no control panel to report to (url=%s, "
                     "token=%s) -- the deadline sweep will record this "
                     "story instead", bool(url), bool(token))
        return False

    data = json.dumps({"verdict": verdict, "review_text": review_text,
                       "reviewer": reviewer}).encode("utf-8")
    req = urllib.request.Request(
        f"{url}?execution_id={execution_id}", data=data, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        logger.error("review: the control panel refused it (%s): %s",
                     exc.code, detail)
    except Exception as exc:                                # noqa: BLE001
        logger.error("review: could not reach the control panel: %s", exc)
    return False


async def synthesise_not_produced(conn, execution_id: int,
                                  reason: str) -> bool:
    """The review nobody wrote, written so the story can still move.

    A REVIEW THAT DID NOT HAPPEN IS A FACT, not an absence. If the
    reviewer burns its legs, wedges, or never calls its finish tool, the
    story is still owed a transition — `done -> reviewed` is gated on
    this row existing. A run that ends without one leaves the story at
    `done` for ever, and with it the thread binding, the protected
    workspace and, until `reclaim` is unblocked, the node.

    IT MUST NOT READ LIKE A PASS. `not_produced` is a verdict of its own
    and renders as one; only `approve` reads as clean. Silently marking
    unreviewed work as reviewed would be worse than the stuck story this
    replaces.
    """
    text = (
        "# Story review — not produced\n\n"
        f"No review was submitted for this story: {reason}.\n\n"
        "This is a placeholder written by the harness so the story could "
        "leave `done`. **It is not an assessment of the work** — nothing "
        "read the diff. Treat this story as unreviewed and look at it by "
        "hand if it matters.\n"
    )
    # WHICHEVER HALF THIS PROCESS IS. A pod may not write the tables,
    # so it reports; the conductor and the panel write directly. Same
    # three-way shape `_store_durably` uses for an emitted artifact, and
    # for the same reason.
    from kernel.workspace import broker, podfs_auth

    url = broker.endpoint(STORY_REVIEW, "STORY_REVIEW_URL")
    token = (os.environ[podfs_auth.ENV_VAR]
             if podfs_auth.ENV_VAR in os.environ else "")
    if not (url and token):
        return await record_review(
            conn, execution_id, verdict="not_produced", review_text=text,
            reviewer="harness")

    import asyncio

    if await asyncio.to_thread(
            report_review, execution_id, verdict="not_produced",
            review_text=text, reviewer="harness"):
        return True

    # THE REPORT FAILED, SO END THE RUN ANYWAY. `kernel.executions` IS
    # this pod's to write, and leaving the phase open would hold the
    # workspace, the admission slot and the node -- `reclaim` stands off
    # while `phase = 'review'`, so nothing else would release them. The
    # story keeps no review here; `review_deadline`'s backstop finds a
    # `done` story with none and records one from outside.
    logger.error("review: could not report not_produced for execution %s; "
                 "ending the phase so the node is released and leaving "
                 "the review to the sweep", execution_id)
    await conn.execute(
        "UPDATE kernel.executions SET state = 'DONE', phase = NULL, "
        "updated_at = now() WHERE id = $1 "
        "AND state NOT IN ('CANCELLED', 'FAILED')", execution_id)
    return False
