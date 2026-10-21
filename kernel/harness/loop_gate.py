"""May this leg start at all? D4 — the kill switch, on the pod's side.

WHAT THE KILL SWITCH IS MADE OF, and it is two gates rather than one.
`kernel/conductor/pauses.py` says it plainly: admission stops NEW work,
and the leg gate stops a RUNNING execution taking its next one. Only the
second stops the bleeding, because everything already admitted runs to
completion at a leg apiece, which is hours. `worker/loop.py` holds the
conductor's copy — one `frozen(conn, goal_id)` above the claim stanza,
gating the schedule sweep, dispatch and leg claiming together.

WHY THE POD NEEDS ITS OWN. That gate works by the conductor DECLINING to
claim the next leg. D2 moves the leg into the execution pod, and a pod
that drives its own legs is no longer asking the conductor's permission
for each one — so the switch silently degrades to "stops new stories
only". An operator who hits Freeze on a runaway story watches it keep
running, keep spending and keep committing, while the banner says
stopped. That is worse than not having the button, because the button
is what somebody reaches for FIRST and it now lies.

BETWEEN LEGS, NOT MID-LEG, and the distinction is load-bearing rather
than cautious. `pauses.py`: "A leg is a model turn and its tool calls;
interrupting one leaves a sandbox in a state nobody described, and the
existing machinery deliberately has no cancellation for exactly that
reason." Freeze means "finish what you are holding and take nothing
new". For the pod's runner, which runs ONE leg per invocation, "between
legs" is the moment after the lock is held and before the first model
call — so this is called there and nowhere else.

AFTER THE LOCK, DELIBERATELY. Reading the switch before taking
`leg_lock` would mean a refusal that another process could contradict a
millisecond later, and worse, a refusal recorded against an execution
this process has no claim on. Under the lock the answer is about a run
nobody else can advance, and the refusal that comes back is the last
word for this tick.

THREE REASONS, NOT ONE, because collapsing them is how a cancelled run
gets reported as frozen and waits for a freeze to be lifted that was
never on. They are checked cheapest-first and terminal-first: an
execution that has left its runnable state is finished with, a
cancellation is a decision already made, and a freeze is the only one of
the three that a later tick may find lifted.

FAIL CLOSED IS INHERITED, NOT RE-DECIDED. `frozen()` returns a synthetic
pause when it cannot read the table — "an unreadable kill switch is not
an absent one" — so a pod whose role lost SELECT on `execution_pauses`
refuses legs rather than running through a freeze it cannot see. That is
the right direction and it has a cost worth naming: a grant mistake
looks like a fleet that quietly stops. `agentos_leg` holds that SELECT
today (`migrations/kernel_execution_pod_leg_role.sql`), and the two
other reads here — `executions` and `proposals` — are granted in the
same file, so all three are inside the audited set rather than beside
it.

WHAT THIS IS NOT. It is not a second copy of the state machine. It
decides one thing, "not this leg, and here is the word for why", and
returns a string the caller turns into `loop_result.refusal`. It writes
nothing: a run that must not take a leg has not failed, has spent no
generation and has journalled nothing, and the process that DECIDED to
freeze or cancel is the one that owns saying so in the row.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Where a leg may legitimately start. RUNNING is the only one `claimable`
# hands out, and PARKED_* is excluded for the reason it gives: a parked
# run is waiting on a human and picking it up defeats the park. Spelled
# here rather than imported from `worker/mailbox.NO_LEG_STATES` because
# that tuple is the complement of a DIFFERENT question — "where can a
# cancellation land without racing a leg" — and the two happen to be
# opposites today. Tying them together would make a change to one a
# silent change to the other.
LEG_STATES = ("RUNNING",)

CANCEL_PENDING_SQL = """
    SELECT payload->>'reason' AS reason
      FROM proposals
     WHERE execution_id = $1 AND kind = 'cancel' AND status = 'PENDING'
     LIMIT 1
"""


async def refuse_reason(conn, execution_id: int) -> str | None:
    """Why this leg must not start, as a `stop_reason`, or None.

    The string is what `loop_result.refusal` records, so it is a token a
    reader can match on rather than a sentence. `not_runnable` carries
    the state it found, because "the run moved while this pod was
    starting" and "somebody cancelled it" are different stories and the
    row is the only place the difference survives.
    """
    row = await conn.fetchrow(
        "SELECT state, goal_id FROM executions WHERE id = $1",
        execution_id)
    if row is None:
        # NOT AN EXCEPTION. A pod outliving the row it was dispatched for
        # is a reaping question, and raising here would turn it into a
        # crash the reader has to read a traceback to understand.
        logger.warning("execution %s no longer exists; refusing the leg",
                       execution_id)
        return "no_such_execution"

    state = row["state"]
    if state not in LEG_STATES:
        logger.info("execution %s is %s, not %s; refusing the leg",
                    execution_id, state, "/".join(LEG_STATES))
        return f"not_runnable:{state}"

    reason = await conn.fetchval(CANCEL_PENDING_SQL, execution_id)
    if reason is not None:
        # The proposal is left PENDING on purpose. `deliver_cancellations`
        # applies it — it checks the run's OWN machine version for a legal
        # edge to CANCELLED and declines rather than forcing one — and
        # this pod has no business making that transition from under it.
        # Declining the leg is the whole of what the pod owes: the run
        # stops advancing, and the conductor finishes the paperwork on a
        # run that now has no leg in flight.
        logger.info("execution %s has a pending cancellation (%s); refusing "
                    "the leg", execution_id, reason or "no reason given")
        return "cancelled"

    from kernel.conductor.pauses import describe, frozen   # noqa: PLC0415

    pause = await frozen(conn, row["goal_id"])
    if pause is not None:
        logger.warning("kill switch: %s — execution %s takes no leg",
                       describe(pause), execution_id)
        return "frozen"
    return None
