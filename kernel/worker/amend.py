"""An amendment: a human changing what a story is for.

The requirement this is built to: *the user talks to the executor for a
story through the assistant, and may later decide they want more —
conveyed at any point, and it has to get through properly.*

"At any point" is the whole of it. A person should not have to know
whether the executor is mid-leg, parked on a question, being verified or
finished, and should not have to pick a different control for each.
There is ONE act.

WHAT AN AMENDMENT CARRIES, any or all:

    scope    appended to the task and delivered as a steer
    checks   a NEW CONTRACT VERSION — added, altered or removed
    and it ALWAYS withdraws the plan approval

The last is not a side effect. A plan approved for the old scope is not
approval for the new one, and `requires_plan` then blocks every write
until the agent registers another and a person approves it. Structural,
not a prompt instruction: the executor cannot proceed on a stale
approval even if it wants to.

WHY A NEW CONTRACT VERSION RATHER THAN AN EDIT. Verdicts are keyed to
claims, and a claim was judged against a specific set of criteria. Edit
one underneath it and the verdict that says "passed" refers to something
that no longer exists — an audit trail that lies about work somebody may
have shipped. A new version keeps every past verdict truthful and says
what changed, when and who changed it.

The executor cannot do any of this, and not because a rule forbids it:
there is no tool. Same argument as `propose_completion` being absent
from the errand surface rather than discouraged in a prompt.

HOW IT LANDS, PER STATE — and none of these are special cases in the
code, they are what the ordinary machinery already does:

    RUNNING      the steer drains at the next step boundary
    PARKED       it waits, and drains at step 0 of the resumed leg
    VERIFYING    it drains on the next leg — and the adjudicator will
                 not let the story reach DONE while it is undelivered
    DONE         the story is reopened, DONE -> PENDING, and it drains
                 on the leg after admission re-dispatches it
"""

from __future__ import annotations

import json
import logging

from kernel.mailbox.api import submit_proposal
from kernel.worker.contracts import new_version

logger = logging.getLogger(__name__)

class AmendRefused(Exception):
    """The amendment cannot be applied, and why."""


async def amend(conn, execution_id: int, scope: str = "",
                checks: list | None = None, by: str = "console") -> dict:
    """Change what this story is for. Works in any state.

    `checks` is the COMPLETE new set when given, not an addition — which
    is what makes editing and removing possible at all. Pass the current
    set with one line changed to change one check.
    """
    row = await conn.fetchrow(
        "SELECT state, task, contract_id, machine_version "
        "FROM executions WHERE id = $1", execution_id)
    if row is None:
        raise AmendRefused(f"no execution {execution_id}")
    scope = (scope or "").strip()
    if row["contract_id"] is None and not checks:
        raise AmendRefused(
            "this story was dispatched with NO checks, so nothing defines "
            "done for it and the adjudicator cannot judge it. Give it "
            "checks here and it gets its first contract.")
    if not scope and checks is None:
        raise AmendRefused(
            "an amendment must change something: new scope, new checks, "
            "or both")

    terminal = row["state"] in ("FAILED", "CANCELLED")
    if terminal:
        raise AmendRefused(
            f"this story is {row['state']}. An amendment asks a run to do "
            f"more, and this one is not going to run again — start a new "
            f"story.")

    # EVERY REFUSAL BEFORE ANY WRITE. The first version issued the new
    # contract version and then discovered the reopen was illegal — so a
    # failed amendment left a finished story pointing at a definition of
    # done nobody had agreed to, which is worse than the amendment not
    # happening at all.
    reopening = row["state"] == "DONE"
    if reopening:
        legal = await conn.fetchval(
            """
            SELECT EXISTS (SELECT 1 FROM run_state_machine m,
                 jsonb_array_elements(m.transitions) t
                WHERE m.version = $1 AND t->>'from' = 'DONE'
                  AND t->>'to' = 'PENDING')
            """, row["machine_version"])
        if not legal:
            raise AmendRefused(
                f"this run was dispatched under machine "
                f"v{row['machine_version']}, which cannot reopen a "
                f"finished story. Carry it forward to the current "
                f"machine first, or start a separate story.")

    actions = []
    contract_id = row["contract_id"]
    if checks is not None:
        note = scope or "checks amended"
        contract_id, _ids = await new_version(
            conn, execution_id, checks, note, by)
        actions.append(f"contract v{contract_id}")

    task = row["task"] or ""
    if scope:
        task = task.rstrip() + (
            "\n\n## Amended by a person, after the work began\n\n"
            + scope
            + "\n\nWhat is already committed passed the checks that were "
              "in force then. Build on it; do not redo it.")

    reopened = reopening
    if reopened:
        # DONE -> PENDING, not RUNNING. The admission slot was released
        # when the story finished, so going back through PENDING
        # re-claims one — or waits for one — and dispatch rebuilds a
        # sandbox against the surviving workspace.
        actions.append("reopened DONE -> PENDING")

    async with conn.transaction():
        # Two statements, because generation must not appear in the
        # first. C1's trigger refuses `generation = current` — it is
        # MONOTONE, not merely non-decreasing — so a
        # `CASE ... ELSE generation END` that leaves it alone is still an
        # UPDATE setting it to what it already is, and the trigger
        # rejects it. Amending a RUNNING story failed outright on that.
        await conn.execute(
            "UPDATE executions SET task = $2, contract_id = $3, "
            "updated_at = now() WHERE id = $1",
            execution_id, task, contract_id)
        if reopening:
            await conn.execute(
                "UPDATE executions SET state = 'PENDING', "
                "generation = generation + 1, container_id = NULL "
                "WHERE id = $1 AND state = 'DONE'", execution_id)

    # ONE proposal carries both halves. `replan` withdraws the plan
    # approval — a plan approved for the old scope is not approval for
    # the new one — and its message is what tells the model to register
    # another. A `steer` alongside it would be a second way to say the
    # same thing, and two ways drift.
    reason = (scope or "the checks for this story have changed")
    if checks is not None:
        reason += (f"\n\nThe definition of done has been revised by {by}. "
                   f"Read the checks in your task; your previous plan was "
                   f"approved against the old ones.")
    await submit_proposal(
        conn, by, "replan", {"reason": reason},
        f"amend-{execution_id}-{contract_id}-{len(task)}", execution_id)
    actions.append("plan approval withdrawn")

    # A story parked on a PLAN APPROVAL has just had that plan
    # superseded: the person is being asked to approve steps for work
    # that is no longer what the story is for. Leaving the park open
    # asks them to rule on a dead question, and approving it would
    # authorise the old scope.
    #
    # Closed as `revise`, not `declined`: the approach was not rejected,
    # the job changed. And `revise` is not permission, so nothing is
    # written until the agent registers a plan for the NEW scope and a
    # person approves that.
    open_plan = await conn.fetchval(
        "SELECT id FROM questions WHERE execution_id = $1 "
        "AND status = 'OPEN' AND kind = 'authorization'", execution_id)
    if open_plan is not None:
        from kernel.harness.park import resolve_request
        await resolve_request(
            conn, open_plan,
            "The story was amended while this plan was waiting, so these "
            "steps are for work it is no longer for. Read the amended "
            "task and register a plan for that.",
            answered_by=by, verdict="revise")
        actions.append(f"the open plan approval (#{open_plan}) was closed "
                       f"as revise — it was for the old scope")

    logger.info("execution %s amended by %s: %s", execution_id, by,
                "; ".join(actions))
    return {"execution_id": execution_id, "action": "amended",
            "reopened": reopened, "contract_id": contract_id,
            "detail": "; ".join(actions)
                      + ". The agent must register a new plan and a person "
                        "must approve it before anything is written."}
