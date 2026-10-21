"""Is anything mid-turn? If so, a refresh waits.

THIS IS THE MEMORY GUARD, NOT A COURTESY. It reads like politeness --
don't cut somebody's reply off mid-sentence -- and that is the smaller
half of it.

The larger half: agents write `MEMORY.md` continuously, and the flush
runs INSIDE a turn, during async compaction. So a gateway stopped while a
turn is in flight can be stopped mid-flush, and a half-written memory
file is the one way this whole procedure can destroy memory even with a
perfect allowlist -- because it has nothing to do with what we write.

No turn in flight means no flush in flight. That is the entire argument,
and it is why the restart step must also stop the gateway GRACEFULLY and
wait for it to drain rather than killing it.

WHERE THE GUARD BELONGS, corrected after the first real run.

The memory risk is at the RESTART, not here. An apply writes AGENTS.md,
SOUL.md, tools and skills over the gateway's RPC API: it never touches a
memory file and never stops a process, so it cannot interrupt a flush.
Stopping the gateway can, and that is a manual step with its own
instructions -- snapshot first, stop gracefully, never kill.

So this check is narrower than it was. What an apply genuinely must
avoid is a turn running AT THAT MOMENT, because tools go live on the
next turn while prompts wait for the restart: a turn straddling the
apply sees new tools described by an old prompt.

QUEUED MESSAGES ARE NOT THAT, and blocking on them made the tool
unusable. The first real run found 30 of them across four goals, the
oldest 26 hours old, with zero agents actually thinking -- a backlog
that never drains. Gating on it meant the fleet was never quiet and the
apply could never run. Pending work is not work in progress, and a queue
nobody is draining is not a reason to refuse for ever.

WHAT COUNTS AS IN FLIGHT:

  run state    `chat_run_state.status = 'thinking'` -- the dispatcher's
               own record of a turn currently running, and the only
               thing that blocks.

               It is a separate table, which an earlier draft got wrong:
               it looked for 'thinking' among the message statuses,
               where it does not exist (`messages_status_chk` permits
               only queued / processing / done / failed). A test caught
               that. Without it this check would have had nothing left
               to block on at all.

WHAT IS REPORTED BUT DOES NOT BLOCK: queued messages, and RUNNING
executions. A leg runs in its own pod and does not read the gateway's
agent config, so refreshing an assistant underneath it changes nothing
about the leg. Both are worth seeing; neither is a reason to refuse.

FAILS CLOSED. If the check cannot run, it reports "busy" rather than
"idle". Refusing a refresh costs a retry; permitting one during a flush
costs an agent's memory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

BUSY_MESSAGE_STATES = ("queued", "processing")

# `chat_run_state` is written by the dispatcher while a turn runs, so
# this catches a turn whose message has already left the queue.
BUSY_RUN_STATES = ("thinking",)

_MESSAGES = """
    SELECT count(*) FROM messages m
      JOIN threads t ON t.id = m.thread_id
      JOIN goals   g ON g.id = t.goal_id
     WHERE m.status = ANY($1::text[]) AND g.status = 'active'
"""

_RUNS = """
    SELECT count(*) FROM chat_run_state r
      JOIN threads t ON t.id = r.thread_id
      JOIN goals   g ON g.id = t.goal_id
     WHERE r.status = ANY($1::text[]) AND g.status = 'active'
"""

_EXECUTIONS = """
    SELECT count(*) FROM kernel.executions WHERE state = 'RUNNING'
"""


@dataclass(frozen=True)
class Quiet:
    """Whether it is safe to write, and what to tell the operator."""

    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


async def is_quiet(conn) -> Quiet:
    """Is any agent mid-turn right now?

    FAILS CLOSED on an unreadable database: refusing costs a retry, and
    the check is cheap enough that a failure here means something else
    is wrong anyway.
    """
    try:
        thinking = await conn.fetchval(_RUNS, list(BUSY_RUN_STATES))
    except Exception as exc:                                # noqa: BLE001
        logger.warning("agent refresh: run-state check failed (%s); "
                       "treating the fleet as busy", exc, exc_info=True)
        return Quiet(False, f"could not tell whether any agent is mid-turn "
                            f"({exc}), so this is being treated as busy")
    if thinking:
        return Quiet(False, f"{thinking} agent(s) are mid-turn. Tools go "
                            f"live on the next turn while prompts wait for "
                            f"the restart, so a turn straddling this would "
                            f"see new tools described by an old prompt. "
                            f"These clear on their own -- try again shortly.")
    return Quiet(True, "no agent is mid-turn")


async def backlog(conn) -> str:
    """What is pending but not blocking, for the report to show.

    Separate from `is_quiet` ON PURPOSE. These numbers are worth seeing
    -- a large queue may mean something is stuck -- but neither is a
    reason to refuse a write that touches no memory and stops no
    process. Reported, never enforced.
    """
    bits = []
    try:
        queued = await conn.fetchval(_MESSAGES, list(BUSY_MESSAGE_STATES))
        if queued:
            bits.append(f"{queued} message(s) queued or processing")
    except Exception:                                       # noqa: BLE001
        pass
    try:
        running = await conn.fetchval(_EXECUTIONS)
        if running:
            bits.append(f"{running} execution(s) RUNNING (their legs run in "
                        f"their own pods and are unaffected)")
    except Exception:                                       # noqa: BLE001
        pass
    return "; ".join(bits)
