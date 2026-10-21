"""I5f — GO asks for an errand it may then run without being asked again.

THE ASK AND THE AUTHORITY ARE TWO ACTS, and they belong to two parties.
GO writes a proposal, which carries no authority at all. A person in the
goal turns it into a grant, and only they can: `errand_schedules` INSERT
is `agentos_human` only and every tool call here wears `agentos_worker`,
so this is a privilege rather than a convention.

WHAT GO IS ACTUALLY ASKING FOR. Not "may I do this work" — GO can already
plan work, and a story or an errand somebody starts needs no new
permission. It is asking for the SECOND approval in advance: may this run
finish without a person accepting it each time. That is the only thing
autonomy adds, and it is the only thing this asks.

WHY IT CANNOT APPROVE ITS OWN. Obvious, and worth saying anyway, because
the shape recurs: an agent that proposes and approves is not a gate with
a step in it, it is a gate with a hinge. Same reasoning as `resolve_request`
refusing an agent's approval (§1.3) and `standing_approval` pinning to a
plan hash.

BOUNDED OR REFUSED. The approving human owns every run — their thread,
their ledger principal, their cost — so the number of runs they are
agreeing to own is part of the question. A proposal with neither a run
ceiling nor an expiry is refused here rather than at the database, so GO
reads why instead of a constraint name.
"""

from __future__ import annotations

import json as _json

import datetime as dt
import logging

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)

# A one-shot IS a schedule that runs once. The sentinel keeps it in the
# same table, the same sweep and the same ceilings as a recurring one —
# `errand_schedules.cron` is NOT NULL with a non-empty CHECK, so "no
# cron" is not expressible and inventing a second table to express it
# would give two answers to what is due.
ONE_SHOT = "@once"

# Long enough that a weekly errand is proposable, short enough that
# nobody is agreeing to a year of unattended runs by not thinking about
# it. A person can always approve another.
MAX_HORIZON_DAYS = 90
MAX_RUNS_CEILING = 100


async def propose_errand(conn, goal_id: str, task: str, rationale: str,
                         name: str = "", domain: str = "ops",
                         # RECURRENCE IS GONE, and these two are kept
                         # only so an older caller does not raise a
                         # TypeError on the way past. They are ignored.
                         # See the note below `ONE_SHOT`.
                         cron: str = ONE_SHOT,
                         max_runs: int | None = None,
                         expires_at: str = "",
                         resources=None,
                         sandbox_size: str = "",
                         proposed_by: str = "go") -> dict:
    """Ask the people in this goal to let an errand run unattended.

    Returns the proposal. A card goes to the goal thread and anybody in
    the goal may decide it — which is wider than the assistant's usual
    "the person whose story this is", and deliberately: an errand GO
    thought of belongs to nobody in particular yet.
    """
    if not (task or "").strip():
        return Refused("E_NO_TASK",
                       "say what the errand should do, in the words the "
                       "executor will be given")
    if not (rationale or "").strip():
        return Refused(
            "E_NO_RATIONALE",
            "say WHY. A person is being asked to let work they did not "
            "think of run without them, and the task text alone does not "
            "tell them whether that is a good idea.")

    # AN ERRAND HAS NO SCHEDULE, AND NO CEILING EITHER.
    #
    # RECURRENCE IS GONE and stays gone: nothing fires an errand on a
    # clock. Sixteen of sixteen grants were `@once`, the approval card
    # described repetition nobody got, and a person was asked to agree
    # to something more open-ended than what they would receive.
    #
    # A CEILING IS A DIFFERENT THING, and pinning it to 1 was wrong.
    # Run 345 was cancelled eighty seconds in, while its pod was still
    # being scheduled, and the errand could not be run again -- the one
    # run it was allowed had been spent on a run that produced nothing.
    # "Run fast tests" is exactly the shape of errand a person wants
    # again tomorrow, and re-approving it each time is a ceremony that
    # protects nobody.
    #
    # So: no timer, no ceiling, ONE AT A TIME. `due` will not claim a
    # schedule whose story still has a live run, so an errand cannot
    # overlap itself however often it is asked for.
    #
    # NOT VALIDATED, IGNORED. A refusal here would make GO retry with a
    # different cron; silently pinning it to one run is what makes the
    # capability gone rather than merely discouraged.
    if cron and cron.strip() not in ("", ONE_SHOT):
        logger.info("errand proposal in %s asked for cron %r; errands run "
                 "once, so it is ignored", goal_id, cron)
    cron = ONE_SHOT
    max_runs = None

    bound = await _bounds(conn, cron, max_runs, expires_at)
    if isinstance(bound, Refused):
        return bound
    runs, until = bound

    # WHICH MACHINE, chosen here because there is nowhere later. An
    # errand's story is created inside `errand_grants.approve` and
    # dispatched immediately -- unlike a story, which is born `proposed`
    # and waits -- so the rung has to travel on the thing being approved.
    #
    # WHICH ALSO MAKES IT PART OF WHAT THE PERSON AGREES TO. The card
    # says what the errand does, how often and how many times; a
    # recurring `large` errand is a different decision from a `small`
    # one, and it must not be possible to make the first while reading
    # the second.
    #
    # REFUSED, NOT ROUNDED DOWN. An unknown word here would otherwise
    # become `small` silently, and an errand that quietly ran on the
    # wrong machine looks merely slow.
    from kernel.conductor.size_choice import valid as _valid_size

    try:
        rung = _valid_size(sandbox_size)
    except ValueError as bad:
        return Refused("E_BAD_SIZE", str(bad))

    # WHAT IT NEEDS, CHECKED NOW. The story is created at APPROVAL and
    # there is no second call, so a proposal that names nothing produces
    # a story nobody can grant to -- which is how KER25's ed57dd54 came
    # to be told "run pytest against multi_agent_dummy_repo" while
    # holding no repository at all.
    from kernel.go.errand_resources import BadResources
    from kernel.go.errand_resources import validate as _validate_resources

    try:
        granted = await _validate_resources(conn, goal_id, resources)
    except BadResources as bad:
        return Refused(bad.code, bad.message)


    row = await conn.fetchrow(
        """
        INSERT INTO kernel.errand_proposals
            (goal_id, proposed_by, domain, task, name, rationale, cron,
             max_runs, expires_at, resources, sandbox_size)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
        RETURNING id, state, cron, max_runs, expires_at, sandbox_size
        """,
        goal_id, proposed_by, domain or "ops", task.strip(),
        (name or "").strip(), rationale.strip(), cron, runs, until,
        _json.dumps(granted), rung)

    logger.info("go: proposed errand %s in %s (%s, max_runs=%s, until=%s)",
                row["id"], goal_id, cron, runs, until)
    return {
        "proposal": row["id"], "goal": goal_id, "state": row["state"],
        "runs": "once" if cron == ONE_SHOT else cron,
        "max_runs": row["max_runs"], "expires_at": row["expires_at"],
        # NAMED BACK, including when it is None: "the default" is an
        # answer, and GO reporting a rung it did not get is how somebody
        # believes an errand is on `medium` for a week.
        "sandbox_size": row["sandbox_size"] or "small (default)",
        "detail": "a card is in the goal thread; anybody in the goal may "
                  "approve it. Until somebody does, nothing runs. The "
                  "first run still parks for plan approval — autonomy "
                  "starts from the run after that.",
    }


async def _bounds(conn, cron: str, max_runs, expires_at: str):
    """(None, expires_at), or a Refused explaining what is wrong with it.

    NO CEILING. The first element is `max_runs`, and it returned 1 --
    which is where run 345's "the grant was for 1 run" came from, since
    the INSERT below writes this value and not the argument. An errand
    stays approved and may run again; what bounds it is the expiry, the
    revocation, and one-at-a-time. See `propose_errand`.

    The only thing left to bound here is how long the PROPOSAL stands
    before it lapses. `cron` and `max_runs` are accepted and ignored.

    THE EXPIRY IS STILL CHECKED, and this is the part that nearly went.
    The old shape returned early for a one-shot -- `return 1,
    _parse_when(expires_at) if expires_at else None` -- so a malformed
    date became None, a date in the past was accepted, and a date ten
    years out was accepted. Harmless while most errands were recurring
    and took the validated path; the moment every errand became a
    one-shot that early return WAS the only path, and three refusals
    became dead code. Removing recurrence would have quietly removed
    expiry validation with it.
    """
    if expires_at in (None, ""):
        return None, None

    until = _parse_when(expires_at)
    if until is None:
        return Refused("E_BAD_EXPIRY",
                       f"{expires_at!r} is not a date; use ISO 8601, "
                       f"e.g. 2026-10-01T09:00:00Z")
    now = dt.datetime.now(dt.timezone.utc)
    if until <= now:
        return Refused("E_EXPIRY_IN_THE_PAST",
                       f"{expires_at} has already passed, so the proposal "
                       f"would lapse before anybody could approve it")
    if until > now + dt.timedelta(days=MAX_HORIZON_DAYS):
        return Refused(
            "E_HORIZON_TOO_FAR",
            f"an expiry more than {MAX_HORIZON_DAYS} days out is a "
            f"proposal nobody will revisit; propose a shorter one and "
            f"ask again")
    return None, until


def _parse_when(raw: str):
    """ISO 8601, tolerant of a trailing Z, aware of nothing else."""
    try:
        when = dt.datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # NAIVE MEANS UTC. Guessing the server's zone would make the same
    # string mean different things in two deployments.
    return (when.replace(tzinfo=dt.timezone.utc)
            if when.tzinfo is None else when)
