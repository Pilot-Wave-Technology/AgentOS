"""H7 — a run that is busy and getting nowhere.

TWO DIFFERENT STALLS, and only one of them looks like a stall.

  LOOPING    steps keep arriving and say the same thing. Measured: a
             real-model baseline ran with the stall clock disabled "so it
             would not interfere" and then looped FORTY-FOUR TIMES saying
             "done". Every budget was healthy. Every predicate was happy.
             The clock would have ended it at eight.
  WEDGED     no steps arrive at all, and the lease stays fresh because the
             heartbeat is a background task that a hung main coroutine
             does not stop. D2's `--hang-after`. Nothing built in Stage C
             can see it: to reconcile it looks alive, because by every
             signal reconcile has, it is.

This module owns the first. The second needs wall-clock evidence from
outside the leg — see `probe_wedged`, which reconcile calls.

THE CLOCK CANNOT BE SWITCHED OFF. `stall_ceiling` may be raised and may
not be set to -1 or 0, and there is no `enabled` flag to find. That is
the same shape as H2's `irreversible`: the setting somebody reaches for
mid-incident is the setting that must not exist, and "so it would not
interfere" is exactly how the 44-loop happened.

WHAT COUNTS AS PROGRESS is a NEW signature, not a new step. Reading two
files is progress; reading the same file twice is not. The signature
follows LR5's normalisation so two adapters phrasing the same call
identically produce the same hash — otherwise a provider that formats
arguments differently would look like progress for ever.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

# Consecutive repeats of one signature before the leg ends. Eight, because
# the measured failure looped 44 times and eight is comfortably inside
# that while leaving room for a legitimate retry-with-the-same-call — a
# flaky test re-run twice is not a stall.
DEFAULT_STALL_CEILING = 8

_CEILING_ENV = "AGENTOS_STALL_CEILING"


def stall_ceiling() -> int:
    """The ceiling for THIS run, resolved at call time.

    A FUNCTION, NOT A DEFAULT ARGUMENT, and `jobs_client._knob` records
    why: a Python default is bound when the function is defined, so
    rebinding a module constant after reading a setting changes nothing.
    `check_stall`'s `ceiling=DEFAULT_STALL_CEILING` was exactly that
    shape, which is how the knob came to exist and configure nothing.

    THE CLOCK CANNOT BE SWITCHED OFF, which this file already says. A
    setting may move the ceiling and may not remove it: anything
    unreadable, absent or below the floor lands on the floor, and the
    floor is 2 because one repeat is a retry and not a loop.
    """
    import os                                              # noqa: PLC0415

    if _CEILING_ENV not in os.environ:
        return DEFAULT_STALL_CEILING
    try:
        return max(2, int(float(os.environ.get(_CEILING_ENV) or 0)))
    except (TypeError, ValueError):
        return DEFAULT_STALL_CEILING

# The operand that identifies WHAT a call acted on, per tool. A tool
# absent here signs on its whole argument set, which is the conservative
# direction: it can only make two calls look MORE different, never less,
# so the clock under-fires rather than over-fires on an unknown tool.
#
# A TUPLE WHERE ONE ARGUMENT IS NOT ENOUGH. `bash_output` is the case:
# `job_id` alone would make every poll of one job identical — which is
# what signing on the whole (constant) arg set already did, and why the
# leg died `stalled` on the eighth poll of a twelve-minute build. The
# question a poll asks is "what is new SINCE x", so the cursor is half the
# operand. A model that advances it is making progress; one that repeats
# the same `since` twenty times is asking one question over and over,
# which is what the clock is for.
_OPERAND = {
    "read": "path", "write": "path", "edit": "path",
    "list_dir": "path", "glob": "pattern", "grep": "pattern",
    "bash": "command", "bash_background": "command",
    "skill": "name", "artifact_read": "artifact_id",
    "story_commit": "message", "ask_human": "question",
    "subagent": "task",
    "bash_output": ("job_id", "since"),
    # BELT AND BRACES. `bash_wait` is meant to be ONE call -- the worker
    # re-arms the slices underneath, so the model does not loop. But if
    # that loop ever breaks out early, a model doing the obvious thing
    # and calling again would sign an identical signature every time and
    # die on the eighth, which is the exact failure the `bash_output`
    # tuple above was added to fix. The cursor is half the operand here
    # for the same reason it is there.
    "bash_wait": ("job_id", "since"),
    # The check's LABEL, not its whole argument set -- which happens to
    # be the same thing today and would stop being it the moment the
    # tool grows a second argument. Keyed explicitly so the clock keeps
    # measuring "asked for the same check again" rather than "sent the
    # same JSON".
    "run_check": "label",
    # `bash_jobs` IS DELIBERATELY ABSENT, and the absence is the right
    # answer rather than an oversight. It takes no arguments, so every
    # call signs identically and eight in a row ends the leg -- which is
    # exactly what eight consecutive "what is running?" calls with
    # nothing done in between ARE. A model watching a job alternates
    # this with `bash_output` or waits with `bash_wait`, and neither
    # trips it.
}


def signature(tool: str, args: Optional[dict] = None) -> str:
    """A stable `tool:operand` hash for one call — LR5's normalisation.

    Promoted out of `test_e5b`, where it lived as a local helper. A rule
    two adapters must agree on cannot be defined inside one test: the
    test then passes because it is checking its own copy.
    """
    # isinstance, not `or {}`: a non-empty STRING is truthy, so a
    # malformed tool call sailed past that guard and raised on the
    # `.get` below. This is a shared helper -- hardening it here is
    # worth more than fixing the one caller that happened to find it.
    args = args if isinstance(args, dict) else {}
    key = _OPERAND.get(tool)
    if isinstance(key, tuple):
        operand = "|".join(str(args.get(k, "")).strip() for k in key)
    elif key is not None:
        operand = str(args.get(key, "")).strip()
    else:
        operand = json.dumps(args, sort_keys=True, separators=(",", ":"))
    raw = f"{tool}:{operand}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class StallResult:
    fired: bool
    predicate: str
    detail: dict


def check_stall(steps: list, ceiling: Optional[int] = None
                ) -> StallResult:
    """Has the run repeated itself past the ceiling?

    CONSECUTIVE, not cumulative. A run that reads the same file at step 3
    and again at step 40 has done thirty-six other things in between and
    is working; a cumulative counter ends it anyway, which is how a clock
    becomes a thing people switch off.

    Empty and boundary steps do not count either way — they are not the
    run repeating itself, and Fb4 already established that a provider
    fact must not be charged to the work.
    """
    if ceiling is None or ceiling < 1:
        # NOT an off switch. -1 means unbounded everywhere else in this
        # tree, and honouring it here would reintroduce the exact
        # condition this module exists to catch.
        #
        # `stall_ceiling()` AND NOT THE CONSTANT, so a compressed run's
        # tighter ceiling actually reaches the one caller. The default
        # argument used to be the constant itself, which meant the
        # setting was read by nobody.
        ceiling = stall_ceiling()

    run = 0
    last = None
    for step in steps:
        if step.get("empty") or step.get("boundary"):
            continue
        sig = step.get("signature")
        if sig is None:
            sig = signature(step.get("tool") or "", step.get("args"))
        if sig == last:
            run += 1
        else:
            run = 1
            last = sig
    fired = run >= ceiling
    return StallResult(
        fired=fired,
        predicate="stalled",
        detail={"repeats": run, "ceiling": ceiling, "signature": last},
    )


async def probe_wedged(conn, execution_id: int, quiet_seconds: float,
                       lease_fresh_seconds: float = 60.0) -> Optional[dict]:
    """Alive by heartbeat, silent by journal — the wedged case (D2).

    Returns a description when the execution has a FRESH lease and has
    written NOTHING for `quiet_seconds`, else None. Both halves are
    required: a stale lease is C6's reconcile problem and a run that is
    journalling is working however slowly.

    THE PROBE MUST NOT ITSELF TRIP DURING COMPACTION. Async compaction
    can hold a leg quiet for a while with nothing wrong, so
    `quiet_seconds` has to sit above the compaction timeout rather than
    above a typical step. Reported, never acted on here: forcing the park
    is the caller's decision, because only the caller knows whether a
    human is already looking at this run.
    """
    row = await conn.fetchrow(
        """
        SELECT
          EXTRACT(EPOCH FROM (now() - l.heartbeat_at)) AS lease_age,
          EXTRACT(EPOCH FROM (now() - COALESCE(
              (SELECT max(ts) FROM step_journal
                WHERE execution_id = $1), l.acquired_at))) AS quiet_for
        FROM execution_leases l
        WHERE l.execution_id = $1 AND l.released_at IS NULL
        """,
        execution_id)
    if row is None:
        return None
    lease_age = float(row["lease_age"] or 0)
    quiet_for = float(row["quiet_for"] or 0)
    if lease_age > lease_fresh_seconds:
        return None                     # not fresh: reconcile's problem
    if quiet_for < quiet_seconds:
        return None
    return {"execution_id": execution_id, "quiet_for": quiet_for,
            "lease_age": lease_age,
            "reason": f"lease refreshed {lease_age:.0f}s ago but nothing "
                      f"journalled for {quiet_for:.0f}s"}
