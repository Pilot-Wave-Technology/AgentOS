"""Ec5 — an errand that runs again, on the tick that already exists.

NO SECOND SCHEDULER. "Only the Conductor dispatches" has survived every
stage, and it survives because exactly one thing turns an intention into a
running container. A scheduler that created containers would be a second
one, and the first time the two disagreed — about a paused goal, an
admission bucket, a kill switch — the schedule would win, silently,
because nothing else would know it had.

So this creates a PENDING execution and stops. Admission, dispatch,
pausing and every ceiling then apply exactly as they do to an errand
somebody started by hand. The schedule decides WHEN and nothing else.

`next_run_at` IS THE CLAIM. The sweep is an `UPDATE … WHERE next_run_at <=
now() RETURNING`, which is atomic: the row moves forward BEFORE the
execution is created, so a second replica running the same instant sees
nothing due. A crash between the two loses one run rather than duplicating
it — the right direction for something whose job might be to send a report
to forty people.

FIVE-FIELD CRON, PARSED HERE. No `pg_cron`, no extension: a deployment
dependency for a five-field string is one somebody discovers in a
clean-room install, and the parser is forty lines.

A MISSED WINDOW RUNS ONCE, NOT N TIMES. A worker down for six hours with a
five-minute schedule wakes to 72 due firings, and running them is a
thundering herd that answers a question nobody asked 71 times. The next
time is computed forward from NOW, so catching up means one run.
"""

from __future__ import annotations

import datetime as dt
import logging

logger = logging.getLogger(__name__)

# How far ahead to search for the next matching minute. Four years covers
# `0 0 29 2 *` — a leap-day schedule — and bounds the loop so an
# unsatisfiable expression fails instead of spinning.
_HORIZON_MINUTES = 366 * 4 * 24 * 60

_FIELDS = ("minute", "hour", "day", "month", "weekday")
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


class BadCron(ValueError):
    """The expression is not a five-field cron this can satisfy."""


def _field(spec: str, low: int, high: int, name: str) -> set:
    """One cron field to the set of values it matches."""
    out: set = set()
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            raise BadCron(f"{name}: empty element in {spec!r}")
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise BadCron(f"{name}: step must be a positive number")
            step = int(raw_step)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise BadCron(f"{name}: {part!r} is not a range")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise BadCron(f"{name}: {part!r} is not a number, range or *")
        if start < low or end > high or start > end:
            raise BadCron(
                f"{name}: {part!r} is outside {low}-{high}")
        out |= set(range(start, end + 1, step))
    if not out:
        raise BadCron(f"{name}: {spec!r} matches nothing")
    return out


def parse(expression: str) -> tuple:
    """Five sets: minute, hour, day-of-month, month, day-of-week."""
    parts = str(expression or "").split()
    if len(parts) != 5:
        raise BadCron(
            f"cron takes five fields — minute hour day month weekday — "
            f"and {expression!r} has {len(parts)}. e.g. `0 9 * * 1` is "
            f"09:00 every Monday.")
    return tuple(_field(p, lo, hi, name)
                 for p, (lo, hi), name in zip(parts, _RANGES, _FIELDS))


def next_after(expression: str, after: dt.datetime) -> dt.datetime:
    """The first matching minute strictly after `after`. UTC throughout.

    DAY-OF-MONTH AND DAY-OF-WEEK ARE OR-ED when both are restricted, which
    is what cron does and what surprises everybody: `0 0 13 * 5` is the
    13th AND every Friday, not Friday the 13th. Matching cron rather than
    being clever means an expression copied from a crontab does the same
    thing here.
    """
    minute, hour, dom, month, dow = parse(expression)
    both_restricted = len(dom) < 31 and len(dow) < 7

    when = after.astimezone(dt.timezone.utc).replace(
        second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(_HORIZON_MINUTES):
        if when.month in month and when.hour in hour and when.minute in minute:
            # Python's Monday=0; cron's Sunday=0.
            weekday = (when.weekday() + 1) % 7
            day_ok = ((when.day in dom or weekday in dow) if both_restricted
                      else (when.day in dom and weekday in dow))
            if day_ok:
                return when
        when += dt.timedelta(minutes=1)
    raise BadCron(
        f"{expression!r} has no next occurrence within four years — check "
        f"the day and month fields, e.g. `0 0 31 2 *` is never")


async def due(conn, limit: int = 20) -> list[dict]:
    """Claim every schedule that is due, atomically. Returns the rows.

    THE UPDATE IS THE CLAIM. Selecting and then updating would let two
    replicas both see the same due row; moving `next_run_at` forward in
    the same statement means the second finds nothing.

    `next_run_at` is set to NULL here and filled in by `advance` once the
    execution exists. A row with a NULL next time is not due, so a crash
    between the two stops that schedule rather than duplicating it — and
    a stopped schedule is visible, where a double-fired one is not.
    """
    rows = await conn.fetch(
        """
        UPDATE kernel.errand_schedules
           SET next_run_at = NULL, last_run_at = now()
         WHERE id IN (
             SELECT id FROM kernel.errand_schedules sch
              WHERE paused = false
                AND next_run_at IS NOT NULL
                AND next_run_at <= now()
                -- ONE AT A TIME. An errand may run as many times as it
                -- is asked to, but never twice at once: two runs of the
                -- same errand share a story, a workspace and a branch,
                -- and the second would be writing under the first. The
                -- claim simply does not fire while a run is live -- it
                -- is not an error and nothing is dropped, because the
                -- request sets `next_run_at` again and this picks it up
                -- on the sweep after the live run reaches a terminal
                -- state.
                AND NOT EXISTS (
                      SELECT 1 FROM kernel.executions x
                        JOIN kernel.run_state_machine m2
                          ON m2.version = x.machine_version
                       WHERE x.story_id = sch.story_id
                         AND NOT (m2.terminal_states @> to_jsonb(x.state)))
              ORDER BY next_run_at
              LIMIT $1
              FOR UPDATE SKIP LOCKED
         )
        RETURNING id, goal_id, story_id, kind, domain, task, name, cron,
                  run_count, max_runs, expires_at,
                  driving_thread_id, approved_by,
                  approved_plan_hash
        """, limit)
    return [dict(r) for r in rows]


def _spent(row) -> str:
    """Why this schedule should not run again, or "" if it should.

    Counted AFTER the run that just happened: `advance` is called once
    per created execution, and `run_count` is still the number BEFORE it.
    So `run_count + 1 >= max_runs` is "that was the last one" rather than
    "one more than allowed has already run".
    """
    import datetime as _dt

    max_runs = row["max_runs"]
    if max_runs is not None and int(row["run_count"] or 0) + 1 >= int(max_runs):
        return f"reached its ceiling of {max_runs} run(s)"
    expires = row["expires_at"]
    if expires is not None and expires <= _dt.datetime.now(_dt.timezone.utc):
        return f"passed its expiry of {expires.isoformat()}"
    return ""


async def advance(conn, schedule_id: int, execution_id: int | None) -> None:
    """Record the run and set the next time. Never raises past the sweep.

    Computed forward from NOW rather than from the missed slot: a worker
    down for six hours with a five-minute schedule wakes to 72 due
    firings, and running them is a thundering herd that answers a question
    nobody asked 71 times.
    """
    row = await conn.fetchrow(
        "SELECT cron, run_count, max_runs, expires_at "
        "FROM kernel.errand_schedules WHERE id = $1", schedule_id)
    if row is None:
        return

    # I1e — THE CEILING, CHECKED WHERE THE NEXT TIME IS SET. `run_count`
    # was incremented by Ec5 and read by nothing, so "every five minutes"
    # authorised 288 runs a day for ever at roughly 10,800 tokens a park.
    #
    # It matters more under the autonomy design than it did then: the
    # approving human is the principal for every autonomous run, so the
    # number of runs a person is accountable for has to be bounded and
    # visible at the moment they approve.
    #
    # Retired rather than paused, with the reason recorded. A schedule
    # that goes quiet with no reason is indistinguishable from a broken
    # sweep, and the first thing anybody does is re-enable it.
    # I5f — A ONE-SHOT HAS NO NEXT TIME, and that is not an error.
    #
    # `errand_schedules.cron` is NOT NULL with a non-empty CHECK, so "no
    # cron" cannot be expressed and a second table for one-shots would
    # give two answers to what is due. `@once` is the sentinel: same
    # claiming, same ceilings, same human-only INSERT.
    #
    # `_spent` would usually catch it first — an approved one-shot carries
    # max_runs=1 — but this must not DEPEND on that, because without it
    # `@once` would reach `next_after`, raise BadCron, and be logged as
    # "an unusable cron" on a row that did exactly what it was meant to.
    # NO TIMER IS NOT SPENT. `@once` means nothing schedules the next
    # run -- it never meant the errand may only ever run once. Retiring
    # the row here is what produced `E_NO_RUN` on run 345: the run was
    # cancelled eighty seconds in, and the errand had already been
    # marked "ran once, as proposed" and paused, so there was no way to
    # run it again and nothing to resume.
    #
    # So the timer is cleared and the GRANT IS LEFT LIVE. `paused` stays
    # false and no `retired_reason` is written, because neither is true;
    # a NULL `next_run_at` is already "not due", which is the whole of
    # what `@once` has to mean. Asking for another run sets it, and the
    # one-at-a-time guard in `due` decides when that run may start.
    if (row["cron"] or "").strip() == "@once":
        logger.info("schedule %s has no timer; run %s recorded, grant stays "
                    "live for another run", schedule_id, execution_id)
        await conn.execute(
            "UPDATE kernel.errand_schedules SET next_run_at = NULL, "
            "run_count = run_count + 1, "
            "last_execution_id = COALESCE($2, last_execution_id) "
            "WHERE id = $1", schedule_id, execution_id)
        return

    reason = _spent(row)
    if reason:
        logger.info("schedule %s has run its course and is retired: %s",
                    schedule_id, reason)
        await conn.execute(
            "UPDATE kernel.errand_schedules SET next_run_at = NULL, "
            "paused = true, retired_reason = $2, run_count = run_count + 1 "
            "WHERE id = $1", schedule_id, reason)
        return
    try:
        following = next_after(row["cron"], dt.datetime.now(dt.timezone.utc))
    except BadCron as bad:
        # PAUSED, NOT RETRIED. An expression that cannot be satisfied will
        # not become satisfiable, and a sweep that recomputes it every
        # four seconds logs the same error for ever.
        logger.error("schedule %s has an unusable cron and is paused: %s",
                     schedule_id, bad)
        # AND ITS NEXT TIME IS CLEARED. A paused row keeping a due time in
        # the past fires the instant somebody un-pauses it, months later,
        # with no relation to the schedule they thought they were
        # resuming.
        await conn.execute(
            "UPDATE kernel.errand_schedules "
            "SET paused = true, next_run_at = NULL WHERE id = $1",
            schedule_id)
        return
    await conn.execute(
        """
        UPDATE kernel.errand_schedules
           SET next_run_at = $2, last_execution_id = COALESCE($3,
                                                    last_execution_id),
               run_count = run_count + 1
         WHERE id = $1
        """, schedule_id, following, execution_id)


async def sweep(conn, limit: int = 20) -> list[dict]:
    """One pass: claim what is due and create a PENDING execution each.

    CREATES A PENDING ROW AND STOPS. That is the whole discipline — the
    Conductor admits and dispatches it exactly as it would an errand
    somebody started by hand, so a paused goal stays paused, the
    admission bucket still counts, and the kill switch still kills. A
    schedule that dispatched would be a second dispatcher, and the first
    time the two disagreed the schedule would win without anything
    knowing.

    Goes through `create_execution` rather than its own INSERT for the
    same reason: that function resolves the profile, so a schedule naming
    a `(kind, domain)` that does not exist is refused here with the list
    of what does — not at dispatch, after a row exists to clean up.
    """
    from kernel.worker.runner import create_execution
    from kernel.worker.schedule_thread import ensure_schedule_thread
    from kernel.workspace.mounts import inherit

    actions = []
    for row in await due(conn, limit):
        execution_id = None
        try:
            # I1d — WITHOUT A STORY NOBODY CAN SEE IT. The thread is
            # derived through the story, so a story-less run has no card,
            # no principal, no controls and no wake. Ec5 passed none, so
            # a scheduled errand that parked for approval was invisible
            # and unanswerable — and an errand parks for approval on its
            # FIRST leg, so that was every scheduled errand.
            # I5b/Ec5 — A THREAD FOR THIS STORY, not whichever one the
            # approver happened to be in. The captured thread is
            # story-scoped, so using it directly posted run 11908's
            # approval card into a FINISHED errand's conversation, where
            # it was correct and invisible. `ensure_schedule_thread`
            # keeps the owner and moves the thread to this story.
            # STILL RUNNING IS NOT AN ERROR. A schedule whose period is
            # shorter than its work comes round while the last run is in
            # flight, and that is the ordinary case, not a fault. The
            # database now refuses the second execution outright
            # (`executions_one_live_per_story`), so without this the
            # sweep would log an exception and record an `error` action
            # every tick for a schedule behaving exactly as configured.
            # Skipped, and `advance` in the `finally` still moves it on.
            from kernel.worker.one_live_run import live_run

            running = await live_run(conn, row["story_id"])
            if running is not None:
                actions.append({"schedule": row["id"], "action": "skipped",
                                "detail": f"execution {running['id']} is "
                                          f"still {running['state']}"})
                logger.info("schedule %s: story %s still running as %s; "
                            "skipping this tick", row["id"], row["story_id"],
                            running["id"])
                continue

            driving = await ensure_schedule_thread(
                conn, row["goal_id"], row["story_id"],
                row["driving_thread_id"]) or row["driving_thread_id"]
            execution_id = await create_execution(
                conn, row["kind"], row["domain"], row["task"], row["goal_id"],
                story_id=row["story_id"],
                # I5b — WHO OWNS THIS RUN. For a grant that is the human
                # who approved it: their thread sees the cards, and
                # `principal_for` makes them the ledger principal and the
                # cost centre. The decision recorded in
                # errand_integration.md is that a person owns every
                # autonomous run of the grant they approved, so this is
                # not an implementation detail — it is the policy.
                driving_thread_id=driving)

            # AND ITS RESOURCES, which nothing else was going to do.
            #
            # `inherit` copies the story's grants into `execution_mounts`,
            # and until now its ONLY caller was the assistant's
            # `start_story`. A scheduled errand is created here and never
            # goes through that path, so it got zero mounts, an empty
            # workspace, and an executor whose only possible plan was
            # "clone the repository" -- which read like a model ignoring
            # its grants and was the truth about what it had been given.
            #
            # A scheduled errand is an ordinary errand that GO supervises
            # rather than a person; it must be provisioned like one.
            try:
                await inherit(conn, execution_id, row["goal_id"],
                              row["story_id"])
            except Exception:                               # noqa: BLE001
                # Loud, and NOT fatal to the run: a workspace that comes
                # up empty is visible in the first plan, whereas losing
                # the run loses the schedule's tick as well.
                logger.exception(
                    "schedule %s: could not inherit resources for story "
                    "%s; its workspace will be empty",
                    row["id"], row["story_id"])

            # AND THE STORY SAYS IT IS RUNNING.
            #
            # A normal errand is IMPORTED, and the import moves its story
            # `accepted -> in_progress`. A scheduled one is never
            # imported, so its story sat at `accepted` for the whole life
            # of every run: the board said "not started" while the work
            # was in flight, which is the difference a person actually
            # notices and the one that made these look broken.
            #
            # Guarded on the same states the import guards on, so a story
            # somebody has already moved is never dragged backwards.
            try:
                await conn.execute(
                    "UPDATE stories SET state = 'in_progress', "
                    "started_at = COALESCE(started_at, now()), "
                    "updated_at = now() "
                    "WHERE id = $1 AND state IN ('proposed', 'accepted')",
                    row["story_id"])
            except Exception:                               # noqa: BLE001
                logger.exception(
                    "schedule %s: could not mark story %s in_progress",
                    row["id"], row["story_id"])
            actions.append({"schedule": row["id"],
                            "execution_id": execution_id,
                            "action": "scheduled",
                            "detail": row["name"] or row["task"][:60]})
            logger.info("schedule %s created execution %s for goal %s",
                        row["id"], execution_id, row["goal_id"])
        except Exception as exc:                            # noqa: BLE001
            # ONE BAD SCHEDULE MUST NOT STOP THE OTHERS. The sweep runs on
            # the worker tick, and an exception here would take down every
            # other schedule and everything else on that tick with it.
            logger.exception("schedule %s could not create an execution",
                             row["id"])
            actions.append({"schedule": row["id"], "action": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
        finally:
            # ALWAYS. `due` set `next_run_at` to NULL as its claim, so a
            # schedule that is not advanced here never runs again — a
            # failure to create one execution would silently retire the
            # schedule, which is a much worse outcome than the failure.
            await advance(conn, row["id"], execution_id)
    return actions
