"""Re-scoping a finished story: more work, same execution, a new contract.

A story that reached DONE satisfied its contract. Wanting more from it
is not a correction — it is the definition of done growing, which is a
new fact about the world rather than a mistake about the old one.

WHY NOT JUST START A NEW STORY. Sometimes you should, and the console
offers that too. But a new execution has an empty journal: it
re-orients from nothing over a workspace it has never seen, and pays
E5c's floor again to rediscover what the last one already knew. When the
new work continues the old work, that cost buys nothing.

The rule of thumb, which the console says out loud: a re-scope when the
work is a continuation, a NEW STORY when it is independent — because a
story is the unit other stories depend on, and one that never ends
cannot be depended on.

WHAT REOPENING IS MADE OF, and almost none of it is new:

  a NEW contract          the old one is satisfied and frozen. Verdicts
                          are keyed to the claim, so "claim 12 passed
                          under contract 5" stays true for ever
  DONE -> PENDING         not RUNNING. The admission slot was released
                          when the story finished; going back through
                          PENDING re-claims one, or waits for one, and
                          re-dispatches a sandbox against the surviving
                          workspace
  a `replan` proposal     which withdraws the plan approval and tells
                          the model to register a new one. Already built
  the task, APPENDED      not replaced. What was asked for the first
                          time is part of the record of why the tree
                          looks the way it does
"""

from __future__ import annotations

import logging

from kernel.mailbox.api import submit_proposal

logger = logging.getLogger(__name__)


class ReopenRefused(Exception):
    """The story cannot be reopened, and why."""


async def reopen(conn, execution_id: int, addendum: str, contract_id: int,
                 actor: str = "console") -> dict:
    """Re-scope a finished story. Returns what happened.

    `contract_id` is required and must be a NEW contract. Reopening
    without one would put the story back to work with its old
    definition of done — which it has already met, so the first thing it
    would do is claim completion again, and E5's fence would refuse the
    claim for carrying an unchanged reason set. Stuck, immediately.
    """
    row = await conn.fetchrow(
        "SELECT state, task, contract_id, machine_version "
        "FROM executions WHERE id = $1", execution_id)
    if row is None:
        raise ReopenRefused(f"no execution {execution_id}")
    if row["state"] != "DONE":
        raise ReopenRefused(
            f"only a DONE story can be re-scoped; this one is "
            f"{row['state']}. A run still working takes a steer, and a "
            f"run waiting on a person takes an answer.")
    if not addendum.strip():
        raise ReopenRefused(
            "say what the extra work is. Reopening with no new scope "
            "puts the story back to work with nothing to do.")
    if contract_id == row["contract_id"]:
        raise ReopenRefused(
            "a re-scope needs a NEW contract. Reopened under the old "
            "one the story has already met its definition of done, so "
            "it would claim completion immediately and E5's fence would "
            "refuse the claim for an unchanged reason set.")

    legal = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM run_state_machine m,
                 jsonb_array_elements(m.transitions) t
            WHERE m.version = $1
              AND t->>'from' = 'DONE' AND t->>'to' = 'PENDING')
        """,
        row["machine_version"])
    if not legal:
        raise ReopenRefused(
            f"machine v{row['machine_version']} has no DONE -> PENDING "
            f"edge. This run was dispatched under rules that do not "
            f"allow re-scoping; start a follow-on story instead.")

    # The task is APPENDED. What was asked the first time is part of the
    # record of why the tree looks the way it does, and a leg that reads
    # only the addendum has no idea what the commits before it were for.
    task = (row["task"] or "").rstrip() + (
        "\n\n## More, added after the first contract was satisfied\n\n"
        + addendum.strip()
        + "\n\nThe work already committed passed its checks. Build on it; "
          "do not redo it.")

    async with conn.transaction():
        await conn.execute(
            "UPDATE executions SET state = 'PENDING', task = $2, "
            "contract_id = $3, generation = generation + 1, "
            "container_id = NULL, updated_at = now() "
            "WHERE id = $1 AND state = 'DONE'",
            execution_id, task, contract_id)

    # A `replan`, PENDING, so the loop delivers it at the first step
    # boundary of the new leg: it withdraws the old plan approval and
    # tells the model to register one for the new scope. Reusing the
    # mechanism rather than adding a second way to say the same thing.
    await submit_proposal(
        conn, actor, "replan",
        {"reason": "the story was re-scoped after its first contract was "
                   "satisfied; the new scope is in your task"},
        f"reopen-{execution_id}-{contract_id}", execution_id)

    logger.info("execution %s re-scoped under contract %s",
                execution_id, contract_id)
    return {"execution_id": execution_id, "action": "reopened",
            "detail": f"DONE -> PENDING under contract {contract_id}; "
                      f"the plan approval is withdrawn and a slot is "
                      f"re-claimed at admission"}
