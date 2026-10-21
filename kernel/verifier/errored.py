"""AN ERROR IS NOT AN ACCEPTANCE — the undecided third answer.

Lifted out of `_apply` unchanged. R1 says an `error` outcome must not
REFUSE a claim, and the obvious reading of that -- `passed = not
refused` -- quietly turned it into the opposite, which is the incident
the comments below describe. It is its own decision with its own
incident, so it is its own file.
"""

from __future__ import annotations

import logging

from kernel.verifier.steer import _tell_executor

logger = logging.getLogger(__name__)


def _failing_command(criterion: dict) -> str:
    """What a person needs to see: the command, not the row it came from.

    Falls back to the label and only then to the id — an id in this
    position reads as part of the error message rather than as a
    reference to a row nobody can look up from a card.
    """
    spec = criterion.get("spec") or {}
    run = (spec.get("run") if isinstance(spec, dict) else "") or ""
    run = " ".join(str(run).split())
    if run:
        return f"`{run[:100]}{'…' if len(run) > 100 else ''}`"
    return str(criterion.get("label") or f"criterion {criterion.get('id')}")


def _first_output_line(outcome) -> str:
    """The first line the check actually printed, when it printed one.

    "command not found" is not actionable; "sh: .venv/bin/pytest: not
    found" is the whole diagnosis. The tail is cheap and it is the
    difference between a card a person can act on and one they have to
    come to us about.
    """
    out = ((outcome.detail or {}).get("output") or "").strip()
    if not out:
        return ""
    return " — " + " ".join(out.splitlines()[0].split())[:160]


async def prepare_failed(conn, row: dict, error: str) -> dict | None:
    """5.8 row 5 — the prepare Job failed, so every criterion is `error`.

    HERE AND NOT IN `rebuild.py` because the answer is this file's
    subject rather than the environment's: a check that could not be
    given its dependencies did not run, so it found nothing wrong. It is
    `error` and never `fail`, and it goes through `errored_claim` below
    so that it inherits the retry bound and the steer -- a genuinely
    broken manifest IS about the work, and the executor is the only party
    who can edit the tree it lives in.
    """
    from kernel.verifier.outcome import unrunnable            # noqa: PLC0415
    from kernel.verifier.writer import load_contract          # noqa: PLC0415

    eid = row["id"]
    contract, criteria = await load_contract(conn, eid)
    if contract is None or not criteria:
        return None
    reason = ("the verification environment could not be built from the "
              "committed manifests: " + (error or "")[-400:])
    claim_seq = await conn.fetchval(
        "SELECT seq FROM step_journal WHERE execution_id = $1 "
        "AND tool = 'propose_completion' ORDER BY seq DESC LIMIT 1", eid)
    return await errored_claim(
        conn, eid, row["generation"], claim_seq or 0,
        [(c, unrunnable(reason, criterion_id=c.get("id"),
                        command="prepare the environment"))
         for c in criteria])


async def errored_claim(conn, eid: int, generation: int, claim_seq: int,
                        results) -> dict | None:
    """The action when a claim's checks could not RUN, else None.

    None means "nothing errored" and the caller carries on to the
    verdict. Everything else here is the retry policy.
    """
    # ── AN ERROR IS NOT AN ACCEPTANCE ─────────────────────────────
    #
    # R1 says an `error` outcome must not REFUSE a claim -- a check
    # that could not run found nothing wrong. But `passed = not
    # refused` quietly turned that into the opposite: a claim whose
    # checks ALL errored (setup could not be prepared, a service
    # never came up, the tree would not archive) wrote passed=true
    # and went to DONE. Nothing ran, and the system called it
    # verified. With environments now built from committed manifests,
    # one typo in requirements.txt was an instant false completion.
    #
    # An errored claim is UNDECIDED: no verdict row is written, the
    # run stays in VERIFYING, and the adjudicator retries next tick
    # -- so a transient (registry outage, docker hiccup) clears
    # itself, a permanent error surfaces on stall_reason for a
    # person, and neither is ever certified as done.
    errored = [(c, o) for c, o in results if o.passes != "pass"]
    if not errored:
        return None
    # NAME THE COMMAND, NOT THE ROW. This printed
    # `label or id`, so a check with no label rendered its
    # PRIMARY KEY: a person was shown "464: command not found"
    # and read it as a shell error about a command called 464.
    # The one fact they needed -- WHICH command -- was sitting
    # in detail["output"] and never reached them.
    #
    # Observed on KER13: the answer was `.venv/bin/pytest: not
    # found`, because the check omitted the `cd` into the repo
    # and there is no venv at the tree root. Thirty seconds to
    # fix, once you can see it.
    reason = "; ".join(
        f"{_failing_command(c)}: "
        f"{(o.detail or {}).get('reason', o.passes)}"
        f"{_first_output_line(o)}"
        for c, o in errored[:3])
    # BOUNDED. An errored claim retries -- a registry outage
    # clears itself -- but a manifest typo retried every tick is
    # an infinite loop burning a prepare container each time,
    # with the executor parked in VERIFYING where it cannot fix
    # anything. Three strikes and the run goes back to the one
    # party that can edit the tree, reason attached.
    strikes = await conn.fetchval(
        "UPDATE executions SET "
        "verify_error_count = verify_error_count + 1, "
        "stall_reason = $2, updated_at = now() "
        "WHERE id = $1 RETURNING verify_error_count",
        eid, f"verification errored (not refused, not passed): "
             f"{reason}"[:500])
    if strikes >= 3:
        detail = (f"verification could not run {strikes} times: "
                  f"{reason}. The environment or checks need a "
                  f"fix in the tree (manifest, .agentos/setup.sh)"
                  f" -- make it, run env_setup, and propose "
                  f"completion again.")
        await conn.execute(
            "UPDATE executions SET state = 'RUNNING', "
            "generation = generation + 1, verify_error_count = 0 "
            "WHERE id = $1 AND state = 'VERIFYING'", eid)
        await _tell_executor(conn, eid, generation, detail, "verr")
        logger.warning("execution %s: claim %s errored %sx -- "
                       "returned to the executor", eid, claim_seq,
                       strikes)
        return {"execution_id": eid, "action": "errored_out",
                "detail": detail[:200]}
    logger.warning("execution %s: claim %s ERRORED -- undecided, "
                   "will retry (%s/3): %s", eid, claim_seq,
                   strikes, reason)
    return {"execution_id": eid, "action": "errored",
            "detail": f"claim {claim_seq}: {len(errored)} "
                      f"criterion(s) could not run -- retry "
                      f"{strikes}/3"}
