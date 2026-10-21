"""I6 — the one way the verifier reaches the executor: a row, not a call.

A FILE OF ITS OWN BECAUSE THREE CALLERS NEED IT AND ONE OF THEM IS A
CYCLE. `verdict.py` steers on a refusal, `errored.py` steers when a
claim could not be judged three times running, and `finish_pass.py` and
`kernel/worker/loop.py` both import it by name from
`kernel.verifier.adjudicator`. Leaving it in either of the first two
would make the other import it back.

The name keeps its leading underscore on purpose: `finish_pass` and the
worker loop already spell it `_tell_executor`, and a second spelling of
the same function is how `worker_tick` came to exist twice.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def _tell_executor(conn, eid: int, generation: int, note: str,
                         tag: str) -> None:
    """The reason, delivered where the executor actually reads: a steer,
    applied at its next step boundary. stall_reason renders for people;
    nothing in the leg pipeline ever showed it to the model, so a
    refused claim used to return the run to RUNNING and the executor
    re-proposed blind."""
    try:
        from kernel.mailbox.api import submit_proposal
        await submit_proposal(
            conn, "verifier", "steer", {"instruction": note[:2000]},
            f"{tag}-{eid}-g{generation}", execution_id=eid)
    except Exception:                                       # noqa: BLE001
        logger.warning("could not steer executor %s with the failure "
                       "reason; stall_reason still carries it", eid)
