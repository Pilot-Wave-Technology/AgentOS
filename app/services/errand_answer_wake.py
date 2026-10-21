"""The executor answered GO. Tell GO.

THE HOLE THIS FILLS. `ask_executor` is the one tool that reaches the
thing actually doing the work, and GO's prompt now sends it there first
on every scheduled check-in. The executor's reply was posted as a
`kernel_answer` card through `notify_new_message`, whose own docstring
says it delivers to "browser subscribers ... WITHOUT waking the agent /
burning a turn".

So GO asked, a person could read the answer, and GO could not. It found
out only if some later, unrelated wake happened to send it looking --
and that wake did not carry the answer either. Watched on run 308: GO
asked at 15:40, the answer landed at 15:41:00, and GO never referred to
it again.

An ask whose answer never arrives is worse than no ask: it spends a turn
and a round trip to the executor, and leaves GO reasoning from the
journal it was told not to trust.

AUTONOMOUS ERRANDS ONLY, and only when GO ASKED. A person's question
answered on a story is not GO's business and waking it to watch is the
waste the whole wake design exists to avoid. Both tests are cheap and
both are read fresh, so revoking a grant stops this on the next answer.

DEDUPED ON THE ANSWER, through the same `go_wake_log` every other wake
uses: `kind='errand_answer'`, `ref='a:<answer id>'`. A replayed sweep
cannot wake GO twice about one reply.

AND IT IS A SWEEP, NOT A HOOK ON THE CARD POSTER, which is where the
first cut put it and where it could never have worked:

    17:24:10 go_wakes: chat queue disabled in this process;
             errand_answer a:26 for goal ERR09 was not delivered

The card posters run in the scheduler's loop and the scheduler has no
`CHAT_QUEUE_ENABLED`, so `enqueue_system_wake` returns None and
`_deliver` releases its claim. A card poster writes a row to `messages`;
a wake producer enqueues a TURN. Those need different things from the
process they run in -- the same lesson the errand watch learned earlier
the same day, from the same comment, which I had read.
"""

from __future__ import annotations

import logging
from app.services.story_card_thread import newest_human_thread

_log = logging.getLogger(__name__)

# How much of the answer rides in the wake. The whole point is that GO
# can act without another call; a truncated answer that sends it back to
# the card would be the same hole in a different shape. Generous, and
# bounded only so one enormous reply cannot dominate the context.
MAX_ANSWER = 4000


def _gap() -> str:
    """A blank line, written without an escape."""
    return chr(10) + chr(10)


def _body(row, approver_name: str = "") -> str:
    """The answer, in the shape every errand wake shares."""
    from kernel.go.wake_format import wake

    q = (row["question"] or "").strip()
    a = (row["answer"] or "").strip()[:MAX_ANSWER]
    return wake(
        header="EXECUTOR REPLIED",
        story=row["story_id"] or "", title=row.get("title") or "",
        approver=approver_name,
        happened="The executor answered the question you asked it.",
        evidence=(f"**You asked:** {q}" + _gap() + "**It says:**"
                  + _gap() + a),
        imperative=(
            "Test that against the approved plan -- this is what the "
            "check-in is for, because a run doing its plan perfectly "
            "and heading somewhere nobody wanted raises no event and "
            "nothing but you will catch it. Matches the plan: say so. "
            "Drifted (a different approach, scope it was not given, a "
            "step skipped, an assumption the plan did not make): "
            "`steer` it back and name the divergence. Stuck: "
            "`run_control`. Needs a person: escalate and say what you "
            "need."),
    )


# Only answers this fresh are worth a turn. An answer from an hour ago is
# not news, and without a bound the first tick after a deploy would wake
# GO once for every answer in the table.
RECENT_S = 900

_PENDING = """
    SELECT a.id, a.execution_id, a.question, a.answer, a.asked_by,
           e.goal_id, e.story_id, s.title,
           COALESCE(
             """ + newest_human_thread("e.story_id") + """,
             s.active_thread_id, s.thread_id) AS thread_id
      FROM kernel.answers a
      JOIN kernel.executions e ON e.id = a.execution_id
      LEFT JOIN public.stories s ON s.id = e.story_id
     WHERE a.card_posted_at IS NOT NULL
       AND a.card_posted_at > now() - ($1 || ' seconds')::interval
     ORDER BY a.id
     LIMIT 20
"""


async def sweep(pool) -> int:
    """Wake GO about any answer it has not been told about yet.

    RUNS WHERE THE CHAT QUEUE IS. `go_wakes` calls this on the same tick
    as the errand watch, for the reason in the module docstring.

    NO SEPARATE "DELIVERED" COLUMN: `go_wake_log` is unique on
    (goal_id, kind, ref) and `_deliver` claims through it, so a second
    pass over the same answer is a no-op and a pass that FAILED to
    enqueue released its claim and will be retried. One source of truth,
    and it is the one every other wake already uses.
    """
    sent = 0
    for row in await pool.fetch(_PENDING, str(RECENT_S)):
        try:
            if await wake_go(pool, row):
                sent += 1
        except Exception:                                   # noqa: BLE001
            _log.exception("errand answer sweep: answer %s failed", row["id"])
    return sent


async def wake_go(conn, row) -> bool:
    """Wake GO with the executor's answer. True when a wake went out."""
    from app.services.errand_thread_visibility import (
        is_autonomous_errand_thread)
    from app.services.go_wake_events import _deliver

    # GO'S OWN QUESTION ONLY. `asked_by` carries the actor that asked --
    # `go:err08` for GO, a human uid otherwise.
    if not str(row["asked_by"] or "").lower().startswith("go"):
        return False
    thread_id = row["thread_id"]
    if not await is_autonomous_errand_thread(conn, thread_id):
        return False
    from kernel.go.wake_format import approver

    try:
        who = await approver(conn, row["goal_id"], row["story_id"] or "")
        return await _deliver(conn, row["goal_id"], "errand_answer",
                              f"a:{row['id']}", _body(row, who), thread_id)
    except Exception:                                       # noqa: BLE001
        # The card is posted and the answer is in the thread; failing to
        # wake GO must not roll that back or stop the sweep posting the
        # next one.
        _log.exception("errand answer wake failed for answer %s", row["id"])
        return False
