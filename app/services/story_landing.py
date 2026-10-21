"""The two ends of `awaiting_merge`: going in, and coming out.

BOTH ENDS IN ONE MODULE deliberately. A state whose entry and exit are
written in different places is one somebody changes half of, and the
half that gets missed here is the exit -- a story that enters and never
leaves holds a pod indefinitely, and nothing about it looks wrong.

WHY THE STORY FOLLOWS THE EXECUTION AND NOT THE OTHER WAY ROUND. The
kernel parks the run; this moves the story. That is not an accident of
layering (though the kernel may not import `app/`, so it could not do
it anyway) -- it is how `done` already works. `on_story_done` is driven
by the same sweep reading the same executions table, for the same
reason: the execution is what actually happened, and the story row is a
projection of it. A projection that writes itself first can disagree
with the thing it projects.

THE COST IS LATENCY, and it is the right thing to spend. The story
shows its old state for up to one poll interval after the run parks.
Against a wait measured in hours or days for a person to merge a PR,
that is nothing -- and in exchange the sweep is a backstop by
construction: a missed NOTIFY, a crashed worker or a torn transaction
all heal on the next pass, because the sweep asks the database what is
true rather than replaying what it was told.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("app.story_landing")

# Executions parked on a landing question whose story has not caught up.
# `LEFT JOIN` on the story so a parked run with a deleted story shows up
# as a NULL rather than vanishing from the sweep silently.
ENTERING_SQL = """
    SELECT e.id AS execution_id, e.story_id, e.goal_id, q.id AS question_id
      FROM kernel.executions e
      JOIN kernel.questions q
        ON q.execution_id = e.id AND q.status = 'OPEN' AND q.kind = 'landing'
      JOIN stories s ON s.id = e.story_id
     WHERE s.state <> 'awaiting_merge'
       AND s.state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')
     ORDER BY q.id
     LIMIT 25
"""

# A parked run whose work has all landed. This is the EXIT, and it is
# written as a sweep rather than only as a callback from the merge for
# the reason in the module docstring: it asks what is true.
#
# ONE QUERY FOR BOTH CALLERS. `$1` NULL is the full sweep; a list of
# story ids is the engine saying "these just merged, look at them now".
# The same predicate answers both, so the engine's fast path and the
# poll backstop can never disagree about what "landed" means -- which
# they would within a week as two queries.
LANDED_SQL = """
    SELECT e.id AS execution_id, e.story_id, q.id AS question_id
      FROM kernel.executions e
      JOIN kernel.questions q
        ON q.execution_id = e.id AND q.status = 'OPEN' AND q.kind = 'landing'
     WHERE ($1::text[] IS NULL OR e.story_id = ANY($1::text[]))
       AND NOT EXISTS (
               SELECT 1 FROM git_node_stories gns
                 JOIN git_nodes gn ON gn.id = gns.git_node_id
                WHERE gns.story_id = e.story_id
                  AND gn.state NOT IN ('MERGED', 'ABANDONED'))
     ORDER BY q.id
     LIMIT 25
"""


# THE COLD PATH. A story whose run was stood down by the deadline
# janitor keeps its `awaiting_merge` state and loses its pod -- which is
# correct, because the branch really is pushed and the PR really is
# open. If somebody merges it a week later there is no park left to
# resolve and no run left to finish, so the hot path above cannot see
# it. Without this it would sit at `awaiting_merge` for ever having
# actually landed, which is the one outcome worse than the leak the
# janitor was written to stop.
COLD_LANDED_SQL = """
    SELECT s.id AS story_id, s.goal_id,
           COALESCE(s.execution_mode, '') = 'local' AS is_local
      FROM stories s
     WHERE s.state = 'awaiting_merge'
       AND ($1::text[] IS NULL OR s.id = ANY($1::text[]))
       AND EXISTS (SELECT 1 FROM git_node_stories gns
                    WHERE gns.story_id = s.id)
       AND NOT EXISTS (
               SELECT 1 FROM git_node_stories gns
                 JOIN git_nodes gn ON gn.id = gns.git_node_id
                WHERE gns.story_id = s.id
                  AND gn.state NOT IN ('MERGED', 'ABANDONED'))
       AND NOT EXISTS (
               SELECT 1 FROM kernel.executions e
                 JOIN kernel.questions q ON q.execution_id = e.id
                WHERE e.story_id = s.id
                  -- BOTH PARKS, not just the landing one. Once the
                  -- merge lands, the run parks again on a `closeout`
                  -- and a person is being asked whether the story is
                  -- finished. Finishing it from here would answer
                  -- their question for them, and answer it the way
                  -- that throws the workspace away.
                  AND q.kind IN ('landing', 'closeout')
                  AND q.status = 'OPEN')
       -- AND NO LIVE RUN AT ALL, which is what this sweep has always
       -- SAID it was for and never actually checked. "No open park"
       -- is true for an instant whenever one park is being swapped for
       -- another, and in that instant this would finish a story whose
       -- run was about to ask the person whether it was finished.
       --
       -- That happened on a4098b67: the trunk sweep retired the
       -- landing park, the merge landed in the same breath, `release`
       -- found no park to answer, and this moved the story to `done`
       -- before the closeout card could be opened. The person was
       -- never asked.
       --
       -- A story that genuinely belongs to this sweep has no live run
       -- by definition: the deadline janitor cancelled it, or (the
       -- local lane) there never was one.
       AND NOT EXISTS (
               SELECT 1 FROM kernel.executions e2
                WHERE e2.story_id = s.id
                  AND e2.state NOT IN ('DONE', 'FAILED', 'CANCELLED'))
     ORDER BY s.updated_at
     LIMIT 25
"""


def _enabled() -> bool:
    """One switch for the whole feature, read from the kernel's own
    definition so the two halves cannot disagree about whether it is on."""
    from kernel.worker.landing import holds_for_merge_enabled
    return holds_for_merge_enabled()


async def _kernel_conn():
    """A connection whose `search_path` reaches the kernel's unqualified names.

    THE CONTROL PANEL'S POOL DOES NOT HAVE IT, and the kernel helpers this
    module calls -- `ask_question`, `open_request`, `finish_execution` --
    speak unqualified `questions` and `executions` against the `kernel`
    schema. On a pooled connection they raise
    `UndefinedTableError: relation "questions" does not exist`.

    Observed live on dev 2026-10-12: the trunk sweep raised this every 30
    seconds and the closeout card could never have been posted. It failed
    CLOSED -- caught per row, nothing written -- so it was noise plus a
    dead feature rather than bad data, but the feature was dead.

    Same shape as `kernel_run_control._kernel_conn` and
    `kernel_decide`; a fresh connection with a server setting, never a
    `SET search_path` on a shared pooled one.
    """
    from app.services.kernel_conn import kernel_conn

    return await kernel_conn()


async def has_unlanded_work(conn, story_id: str) -> bool:
    """Does this story have a branch that has not reached trunk yet?

    THE TEST FOR WHETHER `done` WOULD BE A LIE. A story with no nodes at
    all — a docs story, a story whose work was abandoned — has nothing to
    land and must finish normally; it is `EXISTS` on both halves for that
    reason, not just the NOT EXISTS.

    AN ERRAND HAS A BRANCH AND STILL NEVER LANDS, which is the trap here
    and it is not obvious: `reconcile` cuts a workstream for an errand
    like any other story ("the branch is still cut — that is D1") and
    only declines to create the MERGE story, because its work does not
    land. So the node test alone answers yes for an errand, and marking
    one done would park it waiting for a merge the platform guarantees
    will never happen — until the deadline took it, three days later.

    READ FROM THE STORY ROW, not from `executions.kind`. The kernel's
    `owes_a_landing` prefers the run's own kind, correctly, because the
    two can disagree and the run is what actually happened. There is no
    run here at all — this is the local lane's question — so the row is
    not merely the best available answer, it is the only one.
    """
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM git_node_stories gns
              JOIN git_nodes gn ON gn.id = gns.git_node_id
              JOIN stories s ON s.id = gns.story_id
             WHERE gns.story_id = $1
               AND COALESCE(s.work_kind, 'story') <> 'errand'
               AND gn.state NOT IN ('MERGED', 'ABANDONED'))
        """, story_id))


async def sweep_entering(pool) -> int:
    """Move stories whose run has parked for landing. Returns how many."""
    if not _enabled():
        return 0
    from app.services.story_state_ledger import record_state_pooled

    moved = 0
    for row in await pool.fetch(ENTERING_SQL):
        try:
            # THROUGH THE LEDGER HELPER, never a bare UPDATE. KER12's
            # audit trail read `accepted -> in_progress` and then `done
            # -> reviewed`, with the transition INTO done missing,
            # because one writer took the shortcut. A story recorded as
            # leaving a state it was never recorded as reaching is worse
            # than no record, because the record is trusted.
            landed = await record_state_pooled(
                pool, row["story_id"], row["goal_id"], "awaiting_merge",
                actor_uid="", action="work_pushed_awaiting_merge",
                only_from=("accepted", "in_progress", "blocked"))
            if landed:
                moved += 1
                _log.info("story %s -> awaiting_merge (run %s parked)",
                          row["story_id"], row["execution_id"])
        except Exception:                                   # noqa: BLE001
            _log.exception("story %s could not enter awaiting_merge",
                           row["story_id"])
    return moved


async def sweep_landed(pool, story_ids=None) -> int:
    """Release parked runs whose work is all on trunk. Returns how many.

    `story_ids` is the engine's fast path -- "this node just merged" --
    and None is the poll backstop over everything. The engine knowing
    ONE node merged is not the same as the story having nothing left to
    land, and a multi-repo story proves the difference on its first
    merge, so both callers go through the same predicate rather than
    the engine deciding for itself.
    """
    if not _enabled():
        return 0
    if story_ids is not None:
        story_ids = [s for s in story_ids if s]
        if not story_ids:
            return 0
    released = 0
    for row in await pool.fetch(LANDED_SQL, story_ids):
        try:
            if await release(pool, row["execution_id"], row["question_id"]):
                released += 1
        except Exception:                                   # noqa: BLE001
            _log.exception("execution %s could not be released after its "
                           "work landed", row["execution_id"])
    released += await _sweep_cold(pool, story_ids)
    return released


async def _sweep_cold(pool, story_ids=None) -> int:
    """Finish an `awaiting_merge` story with no live run to finish it.

    TWO DIFFERENT STORIES ARRIVE HERE and the query cannot tell them
    apart, which is fine because the right answer is the same:

      * a POD story whose run the deadline janitor stood down, and whose
        PR somebody merged afterwards;
      * every LOCAL story, which never had a run, a park or a pod — the
        person marked it done, it waited on its PR, and the PR merged.

    Both are "the work is on trunk and nothing is going to finish this
    story but us".

    EXCEPT THAT A LOCAL STORY IS NOT FINISHED BY US. The pod lane has
    nobody left to ask -- its run is gone and its workspace with it -- so
    finishing is the only honest move. The local lane still has the
    person: they are sitting in the editor the work was done in, and
    `done` on a local story runs `finish_local_story` (the result
    summary, the review exemption, the GO's completion message). Moving
    it to `done` from here skips every one of those and takes the click
    away from the one person in a position to make it.

    So a local story is parked at `awaiting_closeout` instead: state
    unchanged, phase saying the merge is in and Mark Done is now live.
    """
    from app.services.story_state_ledger import record_state_pooled

    done = 0
    for row in await pool.fetch(COLD_LANDED_SQL, story_ids):
        if row["is_local"]:
            done += await _offer_closeout(pool, row["story_id"])
            continue
        try:
            # STRAIGHT TO `done`, with no review. There is no workspace
            # for a reviewer to read — either it was released, or (the
            # local lane) it was never ours. Recording the reason on the
            # move is the honest version of that: a story that skipped
            # review and one that passed it are different facts.
            landed = await record_state_pooled(
                pool, row["story_id"], row["goal_id"], "done",
                actor_uid="", action="landed_with_no_live_run",
                extra_set="completed_at = now()",
                only_from=("awaiting_merge",))
            if landed:
                done += 1
                _log.info("story %s landed with no live run — done",
                          row["story_id"])
        except Exception:                                   # noqa: BLE001
            _log.exception("story %s landed cold but could not finish",
                           row["story_id"])
    return done


async def _offer_closeout(pool, story_id: str) -> int:
    """Tell a local story its work is in, and leave the rest to a person.

    IDEMPOTENT AND NARROW: the phase only moves off `awaiting_merge`, so
    a story a person is already closing out, or has moved on from with
    more work, is not dragged backwards by the next sweep tick.
    """
    try:
        moved = await pool.fetchval(
            "UPDATE stories SET current_phase = 'awaiting_closeout', "
            "       updated_at = now() "
            " WHERE id = $1 AND state = 'awaiting_merge' "
            "   AND COALESCE(current_phase, '') = 'awaiting_merge' "
            " RETURNING id", story_id)
    except Exception:                                       # noqa: BLE001
        _log.exception("local story %s landed but could not be offered "
                       "closeout", story_id)
        return 0
    if not moved:
        return 0
    _log.info("local story %s landed — offering closeout", story_id)
    # THE CARD IS SEPARATE, because the state is what matters and the
    # notice is what can fail. Folding them into one `try` meant a thread
    # that could not be written reported the sweep as having done nothing
    # -- after it had already moved the phase -- so the next tick found
    # the story moved on and never retried, and the count the poll logs
    # was a lie about work it HAD done.
    try:
        from app.services.local_report import report_message  # noqa: PLC0415
        await report_message(
            story_id, "agent",
            "Every lane has merged. The story is ready to close out — mark "
            "it done, or ask for more work and it starts a new plan.",
            # THE CARD SAYS "ask for more work" SO IT HAD BETTER OFFER
            # IT. With only `kind` the Slack renderer had nothing to
            # address and fell through to read-only, which left the
            # prose above describing an action the reader could not
            # take anywhere but the `/run more` slash command.
            #
            # `story_id` is what the button carries; the option list is
            # what `go_cards._render_decidable` turns into one. Asking
            # for more work needs a sentence, so the button opens a
            # modal rather than acting on the click.
            card_data={"kind": "closeout_offer", "story_id": story_id,
                       "options": [{"id": "more", "value": "more",
                                    "label": "Ask for more work"}]})
    except Exception:                                       # noqa: BLE001
        _log.exception("local story %s moved to closeout but the notice "
                       "could not be posted", story_id)
    return 1


async def release(pool, execution_id: int, question_id: int) -> bool:
    """Answer the landing park with the merge, and let the run finish.

    ONE TRANSACTION for the answer and the finish. Torn, the two halves
    are both bad in their own way: an answered question with a still-
    parked run is a wait nothing will ever end, and a finished run with
    an open question trips `questions_one_open` the next time anything
    asks. Together they are the single fact "it landed".
    """
    from kernel.worker.finish import finish_execution

    conn = await _kernel_conn()
    try:
        async with conn.transaction():
            # `status = 'OPEN'` is the CAS. Two sweeps racing -- the engine's
            # callback and the poll backstop, which is exactly the pair this
            # is built from -- must not both finish the run.
            answered = await conn.execute(
                "UPDATE kernel.questions "
                "   SET status = 'ANSWERED', answer_text = $2, verdict = 'approve', "
                "       answered_by = 'git-engine', answered_at = now() "
                " WHERE id = $1 AND status = 'OPEN'",
                question_id,
                "The work reached trunk. Nobody was asked: a merge is the "
                "answer to this park.")
            if answered.split()[-1] == "0":
                return False

            # ── AND NOW A PERSON DECIDES, ONE LAST TIME ──────────────────
            #
            # The work is on trunk and the workspace is STILL UP. That is
            # the only moment at which "actually, one more thing" can
            # continue this story rather than start another, so it is the
            # moment to ask — and closing the story is what releases the
            # tree, which makes it a decision with a cost rather than a
            # formality.
            #
            # NOT THE SAME QUESTION AS THE ACCEPTANCE. That one asked about
            # work that was pushed; this one is asked about work that
            # landed, and in between a person may have seen it on trunk.
            if await _open_closeout(conn, execution_id):
                return True

            # The closeout could not be opened — a malformed ask, or a
            # second one racing. Finishing is the safe fallback: a run that
            # cannot ask must not be left parked on a question nobody will
            # see. `owes_a_landing` is false by construction here, so this
            # takes the review-or-DONE branch it would have taken at
            # acceptance.
            moved = await finish_execution(
                conn, execution_id, only_from=("PARKED_HOT", "PARKED_COLD"))
    finally:
        await conn.close()

    if moved:
        _log.info("execution %s landed — released from its landing park",
                  execution_id)
    else:
        # The question is answered and the run did not move, which means
        # it was cancelled or failed while parked. Nothing is stuck: the
        # park is closed and the run is already terminal.
        _log.info("execution %s was no longer parked when its work landed",
                  execution_id)
    return bool(moved)


# A run that a person sent back to work from the closeout card. The
# kernel un-parked it and withdrew the plan; what is left is on this
# side of the boundary -- the next node, and the story row.
#
# FOUND BY SHAPE, not by a flag. A RUNNING execution whose newest
# closeout was answered `revise`, on a story still sitting at
# `awaiting_merge`, can only be this. A flag would need somebody to
# clear it, and the one that never gets cleared is the one that turns a
# sweep into a loop.
RESUMED_SQL = """
    SELECT e.id AS execution_id, e.story_id, s.goal_id
      FROM kernel.executions e
      JOIN stories s ON s.id = e.story_id
     WHERE e.state = 'RUNNING'
       AND s.state = 'awaiting_merge'
       AND (SELECT q.verdict FROM kernel.questions q
             WHERE q.execution_id = e.id AND q.kind = 'closeout'
             ORDER BY q.id DESC LIMIT 1) = 'revise'
     ORDER BY e.id
     LIMIT 25
"""


async def sweep_resumed(pool) -> int:
    """Finish the app-side half of "more work after the merge"."""
    if not _enabled():
        return 0
    from app.services.git_graph.node_cycles import reopen_for_next_cycle
    from app.services.story_state_ledger import record_state_pooled

    moved = 0
    for row in await pool.fetch(RESUMED_SQL):
        try:
            async with pool.acquire() as conn, conn.transaction():
                # IDEMPOTENT: the landings row conflicts and the reset
                # is guarded on `state = 'MERGED'`, so a second pass
                # over the same row does nothing.
                #
                # A POOLED CONNECTION IS RIGHT HERE, unlike the two
                # sweeps below: `reopen_for_next_cycle` touches only
                # public-schema tables, so it needs no kernel
                # search_path.
                await reopen_for_next_cycle(conn, row["story_id"])
            await record_state_pooled(
                pool, row["story_id"], row["goal_id"], "in_progress",
                actor_uid="", action="more_work_after_merge",
                only_from=("awaiting_merge",))
            moved += 1
            _log.info("story %s back to work after its merge (run %s)",
                      row["story_id"], row["execution_id"])
        except Exception:                                   # noqa: BLE001
            _log.exception("story %s was sent back to work but its next "
                           "cycle could not be cut", row["story_id"])
    return moved


# A story waiting to merge whose PR a person CLOSED instead.
#
# AFTER THE ENGINE'S DEBOUNCE, not instead of it. `detect` already waits
# out a close-then-reopen before cancelling a node, because GitHub
# allows reopening and a card sent on the first `closed` read would ask
# about something that un-happened. By the time the node is ESCALATED
# with this error, that wait is over and the close is real.
PR_CLOSED_SQL = """
    SELECT e.id AS execution_id, e.story_id, q.id AS question_id
      FROM kernel.executions e
      JOIN kernel.questions q
        ON q.execution_id = e.id AND q.status = 'OPEN' AND q.kind = 'landing'
     WHERE EXISTS (
               SELECT 1 FROM git_node_stories gns
                 JOIN git_nodes gn ON gn.id = gns.git_node_id
                WHERE gns.story_id = e.story_id
                  AND gn.state = 'ESCALATED'
                  AND gn.last_error LIKE 'PR closed unmerged%')
       -- AND NOTHING ELSE STILL IN FLIGHT. A multi-repo story whose
       -- second repo is still open has not stopped; asking now would
       -- offer to cancel a story that is still landing elsewhere.
       AND NOT EXISTS (
               SELECT 1 FROM git_node_stories gns
                 JOIN git_nodes gn ON gn.id = gns.git_node_id
                WHERE gns.story_id = e.story_id
                  AND gn.state NOT IN ('MERGED', 'ABANDONED', 'ESCALATED'))
     ORDER BY q.id
     LIMIT 25
"""


async def sweep_pr_closed(pool) -> int:
    """Ask what a closed-unmerged PR was meant to mean."""
    if not _enabled():
        return 0
    asked = 0
    for row in await pool.fetch(PR_CLOSED_SQL):
        # A KERNEL CONNECTION, not a pooled one: `_open_pr_closed` reaches
        # `open_request` -> `ask_question`, which writes unqualified
        # `questions`. See `_kernel_conn`.
        conn = await _kernel_conn()
        try:
            async with conn.transaction():
                # The landing will never be answered by a merge that is
                # not coming. Closing it is what frees the slot for the
                # question that CAN be answered -- `questions_one_open`
                # allows exactly one.
                closed = await conn.execute(
                    "UPDATE kernel.questions "
                    "   SET status = 'ANSWERED', verdict = 'decline', "
                    "       answer_text = $2, answered_by = 'git-engine', "
                    "       answered_at = now() "
                    " WHERE id = $1 AND status = 'OPEN'",
                    row["question_id"],
                    "The PR was closed without being merged, so this wait "
                    "has no answer coming.")
                if closed.split()[-1] != "0" and await _open_pr_closed(
                        conn, row["execution_id"]):
                    asked += 1
        except Exception:                                   # noqa: BLE001
            _log.exception("execution %s: could not ask about its closed PR",
                           row["execution_id"])
        finally:
            await conn.close()
    return asked


async def _open_pr_closed(conn, execution_id: int) -> bool:
    """Ask the person what closing the PR meant. True if asked."""
    from kernel.harness.park_open import open_request

    row = await conn.fetchrow(
        "SELECT generation, story_id FROM kernel.executions WHERE id = $1",
        execution_id)
    if row is None:
        return False

    closed = await conn.fetch(
        "SELECT DISTINCT gn.branch_name, gn.pr_number FROM git_node_stories gns "
        "  JOIN git_nodes gn ON gn.id = gns.git_node_id "
        " WHERE gns.story_id = $1 AND gn.state = 'ESCALATED' "
        "   AND gn.last_error LIKE 'PR closed unmerged%'", row["story_id"])
    named = ", ".join(
        f"{r['branch_name']}" + (f" (PR #{r['pr_number']})" if r["pr_number"]
                                 else "") for r in closed) or "its branch"

    result = await open_request(
        conn, execution_id, row["generation"], "pr_closed",
        {"question": ("The PR for this story was closed without being "
                      "merged. Keep working on it, or cancel the story?"),
         "summary": (f"Closed without merging: {named}. The work is still "
                     f"on the branch and the workspace is still up."),
         "branches": [r["branch_name"] for r in closed if r["branch_name"]],
         "reversible": True},
        content_hash=f"pr-closed-exec-{execution_id}")

    if not isinstance(result, int):
        _log.warning("execution %s: could not ask about its closed PR (%s)",
                     execution_id, getattr(result, "code", result))
        return False
    _log.info("execution %s: asked what the closed PR meant (question %s)",
              execution_id, result)
    return True


async def _open_closeout(conn, execution_id: int) -> bool:
    """Ask the person whether the story is finished. True if asked.

    REFUSING TO ASK IS NOT AN ERROR, and the caller treats it as "finish
    instead". `ask_question` returns an `Invalid` rather than raising,
    and a run that cannot ask must still be allowed to end — a story
    parked on a question that was never posted is worse than one that
    closed without the last word.
    """
    from kernel.harness.park_open import open_request

    row = await conn.fetchrow(
        "SELECT generation, story_id FROM kernel.executions WHERE id = $1",
        execution_id)
    if row is None:
        return False

    merged = [r["branch_name"] for r in await conn.fetch(
        "SELECT DISTINCT gn.branch_name FROM git_node_stories gns "
        "  JOIN git_nodes gn ON gn.id = gns.git_node_id "
        " WHERE gns.story_id = $1 AND gn.state = 'MERGED' "
        " ORDER BY gn.branch_name", row["story_id"]) if r["branch_name"]]

    result = await open_request(
        conn, execution_id, row["generation"], "closeout",
        {"question": ("This story's work is merged. Close the story, or "
                      "ask for more work while the workspace is still up?"),
         # `summary` is what `validate_question` requires of a closeout:
         # a card asking somebody to call something finished has to say
         # what it is.
         "summary": ("The work is on trunk"
                     + (" (" + ", ".join(merged) + ")" if merged else "")
                     + ". Closing the story releases its workspace."),
         "branches": merged,
         "reversible": False},
        # ONE PER RUN. A re-detected merge -- the webhook and the poll
        # both finding it, which is the normal case -- must not open a
        # second card asking the same question.
        content_hash=f"closeout-exec-{execution_id}")

    if not isinstance(result, int):
        _log.warning("execution %s landed but could not ask for closeout "
                     "(%s) — finishing instead", execution_id,
                     getattr(result, "code", result))
        return False
    _log.info("execution %s landed — asked for closeout (question %s)",
              execution_id, result)
    return True


async def on_node_landed(pool, node_id: str) -> int:
    """The engine's callback, called when a node reaches MERGED.

    BEST-EFFORT BY CONTRACT. `_finalize_merged` calls this outside its
    transaction, beside the branch cleanup, because the commits are
    already on trunk by then and nothing here may hold row locks for a
    round-trip. If it fails or never runs, `sweep_landed` finds the same
    rows on the next pass -- late rather than never.
    """
    if not _enabled():
        return 0
    riders = [r["story_id"] for r in await pool.fetch(
        "SELECT story_id FROM git_node_stories WHERE git_node_id = $1",
        node_id)]
    return await sweep_landed(pool, riders)
