"""Changing a story changes BOTH records, or it changes neither.

THE HOLE THIS CLOSES. A story is written down twice and the two halves
had no join:

  * `stories.description` / `.acceptance_criteria` — the board's record,
    written by `POST …/stories/{sid}/modify`, which is a GO endpoint and
    knows nothing about runs.
  * `executions.task` and the contract's checks — what the executor is
    actually working to, written by `kernel/worker/amend.py`, which
    knows nothing about the story row.

Update one and the other still says the old thing. That is not a
tidiness problem: the STORED STORY TEXT OUTRANKS IN-FLIGHT CHAT at
every leg boundary, because a thin rehydration reads the record rather
than the conversation. Run 429 kept reverting a correction for exactly
this reason — the person said it, the executor heard it, and the next
leg read the unamended story and undid it. The only fix that worked was
deleting the story and recreating it with the corrected text.

SO BOTH HALVES COMMIT TOGETHER, in one transaction on one connection.
Half an amendment is worse than none: it produces two records that
disagree, and the one that wins is the one nobody edited.

WHY IT WITHDRAWS THE PLAN. `amend` already does, and the reason is the
same one that runs through this whole lifecycle: a plan approved for
the old scope is not approval for the new one. Changing what a story is
for and leaving its definition of done in place is how work gets
measured against a contract nobody agreed to.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("app.story_amend")

# The story-row fields this owns. Anything else on the story -- assignee,
# priority, dependencies -- is planning metadata the executor does not
# read, so it needs no join and stays where it is.
STORY_FIELDS = ("description", "acceptance_criteria")

# Parks that a change of scope makes meaningless. Each is a question
# about a story that no longer exists in the form it was asked about.
_SUPERSEDED_PARKS = ("landing", "closeout", "pr_closed")


class AmendRefused(Exception):
    """The amendment cannot be applied, and why."""


async def live_execution(conn, story_id: str):
    """The run this story's work is in, or None."""
    return await conn.fetchrow(
        "SELECT id, state FROM kernel.executions "
        " WHERE story_id = $1 AND state NOT IN ('FAILED', 'CANCELLED') "
        " ORDER BY id DESC LIMIT 1", story_id)


async def amend_story(goal_id: str, story_id: str, actor_uid: str, *,
                      description: str | None = None,
                      acceptance_criteria: str | None = None,
                      checks: list | None = None) -> dict:
    """Change a story and the run working to it, together.

    `checks` is the COMPLETE new set when given, never an addition —
    `amend`'s rule, kept rather than restated differently, because two
    descriptions of one convention is how they come apart.
    """
    from app.services.kernel_run_control import _kernel_conn
    from kernel.worker.amend import AmendRefused as KernelAmendRefused
    from kernel.worker.amend import amend as kernel_amend

    fields = {k: v for k, v in (("description", description),
                                ("acceptance_criteria", acceptance_criteria))
              if v is not None}
    if not fields and checks is None:
        raise AmendRefused(
            "an amendment must change something: a description, "
            "acceptance criteria, or the checks.")

    conn = await _kernel_conn()
    try:
        story = await conn.fetchrow(
            "SELECT id, title, state, story_kind FROM stories "
            " WHERE id = $1 AND goal_id = $2", story_id, goal_id)
        if story is None:
            raise AmendRefused("That story is not in this goal.")
        if (story["story_kind"] or "code") == "merge":
            raise AmendRefused(
                "Merge stories are managed by the Git Engine and cannot be "
                "edited.")
        if story["state"] in ("cancelled", "rejected"):
            raise AmendRefused(
                f'"{story["title"]}" is {story["state"]}. An amendment asks '
                f"for more work, and this story is not going to do any — "
                f"start a new one.")

        run = await live_execution(conn, story_id)

        # EVERY REFUSAL BEFORE ANY WRITE, which is `amend`'s own rule and
        # matters more here: this writes two records, so a refusal
        # discovered halfway leaves exactly the disagreement the whole
        # module exists to prevent.
        async with conn.transaction():
            if fields:
                sets = ", ".join(f"{f} = ${i + 1}"
                                 for i, f in enumerate(fields))
                await conn.execute(
                    f"UPDATE stories SET {sets}, updated_at = now() "
                    f" WHERE id = ${len(fields) + 1}",
                    *fields.values(), story_id)

            detail = "the story record was updated"
            if run is not None:
                # SCOPE IS THE DESCRIPTION, and deliberately so. The
                # executor reads `executions.task`; the person edited the
                # story. Sending anything else would make the two records
                # differ again by a different route.
                scope = _scope_text(fields)
                try:
                    got = await kernel_amend(
                        conn, run["id"], scope=scope, checks=checks,
                        by=f"human:{actor_uid}" if actor_uid else "assistant")
                    detail = got.get("detail") or detail
                except KernelAmendRefused as exc:
                    # THE WHOLE THING ROLLS BACK. A story row carrying
                    # the new text with a run still working to the old
                    # one is the disagreement this exists to prevent, and
                    # it is worse than the edit simply not landing.
                    raise AmendRefused(
                        f"The story record was NOT changed, because the run "
                        f"could not be amended to match: {exc}") from exc

                await _close_superseded_parks(conn, run["id"], actor_uid)
    finally:
        await conn.close()

    _log.info("story %s amended by %s (run %s): %s", story_id,
              actor_uid or "assistant",
              run["id"] if run else "none", detail)
    return {"ok": True, "story_id": story_id,
            "execution_id": run["id"] if run else None,
            "updated": sorted(fields) + (["checks"] if checks else []),
            "detail": detail}


def _scope_text(fields: dict) -> str:
    """What the executor is told changed, in the person's own words."""
    parts = []
    if fields.get("description"):
        parts.append(str(fields["description"]).strip())
    if fields.get("acceptance_criteria"):
        parts.append("Acceptance criteria are now:\n"
                     + str(fields["acceptance_criteria"]).strip())
    return "\n\n".join(parts)


async def _close_superseded_parks(conn, execution_id: int,
                                  actor_uid: str) -> None:
    """Retire parks asking about a story that has just changed.

    A run parked on a `closeout` is being asked whether the story is
    finished. Amending it answers that: no. Leaving the park open asks a
    person to rule on a question about a story that no longer exists in
    the form it was asked about — and `questions_one_open` would refuse
    the next park while it sat there.

    MARKED ANSWERED DIRECTLY rather than through `resolve_request`,
    which is the one place this deviates from the house style and the
    reason is concrete: `resolve_request`'s arms for these kinds submit
    their own `replan`, and `amend` has already submitted one. Two
    withdrawals of the same approval reach the model as two separate
    instructions to re-plan, and it is not obvious to it that they are
    one event.
    """
    closed = await conn.fetch(
        "UPDATE kernel.questions "
        "   SET status = 'ANSWERED', verdict = 'revise', "
        "       answer_text = $3, answered_by = $2, answered_at = now() "
        " WHERE execution_id = $1 AND status = 'OPEN' "
        "   AND kind = ANY($4::text[]) "
        " RETURNING id",
        execution_id, f"human:{actor_uid}" if actor_uid else "assistant",
        "The story was amended while this was waiting, so it is asking "
        "about work the story is no longer for.",
        list(_SUPERSEDED_PARKS))
    for row in closed:
        _log.info("execution %s: closed park %s — the story was amended",
                  execution_id, row["id"])
