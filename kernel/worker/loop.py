"""H0w — the worker loop. The link that was never built.

`dispatch()` creates the sandbox, claims the lease, and stops. Nothing then
runs the harness: `leg_runner` is a CLI that only test scripts invoke, and
`grep run_leg kernel/conductor/` comes back empty. So the Conductor and the
executor have never been connected, and every claim Stages E and Eb make
was proven by a test calling `run_leg` directly.

ONE LEG PER TICK. The leg is the unit the whole design is built around —
predicates end it, the brief carries across it, the generation fences it.
A worker that ran a whole story inside one tick would be a second scheduler
with none of that.

WHAT ENDS A RUN, per profile rather than per branch:

  errand  its declared terminal command's exit status is the verdict (R3).
          Exit 0 -> DONE. Non-zero -> the agent gets another leg to fix it,
          until budget or the leg ceiling -> FAILED. Those are the other
          terminal predicates every execution already has.

  story   `propose_completion` moves it to VERIFYING and the worker stops
          there. Nothing adjudicates yet — that is Stage F's, and waiting
          is the correct behaviour rather than a gap to paper over. A story
          therefore runs, works, commits, and waits.
"""

from __future__ import annotations

import asyncio
import logging

from kernel.harness.errand import apply_verdict, verdict_from_exit, verdict_from_predicate
from kernel.harness.models import health as _health
from kernel.harness.models.breaker import ProviderUnavailable
from kernel.harness.park import plan_is_approved
from kernel.profiles.registry import resolve
from kernel.worker.advance import advance_one
from kernel.worker.lease import leg_lock
# Re-exported, not re-implemented. `ensure_reachable` moved to
# `worker/reachability.py` with the C5 fix -- it is the code that
# destroys a live pod, and it now has a file that says so at the top.
# The name stays importable here because `infra/e0/ci` and the
# console import it from the loop.
from kernel.worker.reachability import ensure_reachable  # noqa: F401
from kernel.answerer.deliver import deliver_questions
from kernel.conductor.pauses import describe, frozen
from kernel.worker.mailbox import (deliver_answers, deliver_cancellations,
                                   expire_stale_holds)

logger = logging.getLogger(__name__)

# A run that has taken this many legs is not converging. Distinct from the
# per-leg step budget: that bounds one leg, this bounds the run.
# The story ceiling. Configuration, not a constant: it is coupled to leg
# SIZE, and changing one without the other is the trap. Halve the tokens
# per leg and a story that finished in five legs needs ten; leave the
# ceiling at 12 and it fails for a reason that has nothing to do with
# the work. `LegBudget.for_testing()` raises both together.
def max_legs() -> int:
    """Read at each use, so a config change takes effect without a restart."""
    from kernel.harness.models.config import choose_leg

    return choose_leg().max_legs


async def _clear_publish_streak(conn, execution_id: int) -> None:
    """A delivered push ends the forge-refusal streak.

    Called on BOTH success paths. A streak left behind would park the
    next unrelated refusal immediately, one failure early, which is
    precisely the interruption `PARK_AFTER = 2` exists to avoid.
    """
    from kernel.worker import publish_block
    await publish_block.clear_forge_refusals(conn, execution_id)


async def claimable(conn, goal_id: str | None = None) -> list[dict]:
    """Executions that are dispatched, running, and not waiting on anyone.

    IN THE ORDER THE NEXT SLOT SHOULD BE OFFERED (C3). This used to end
    `ORDER BY e.id` and the caller then took `rows[:max_concurrent]`, so
    the lowest id re-won the slot it had just freed on every pass, for
    ever: with more runnable executions than slots, the high-id stories
    never ran at all. The rule now is least-recently-advanced, keyed on
    `max(step_journal.ts)` — activity the journal already records — with
    `id` breaking ties deterministically. See `worker/fairness.py` for why
    it is derived from existing data rather than a new column, and why the
    order is decided in Python rather than in `ORDER BY`.

    PARKED_* is excluded deliberately: a parked run is waiting on a human
    and picking it up would defeat the park. It re-enters when the answer
    arrives and `resolve_request` returns it to RUNNING.
    """
    from kernel.worker import fairness

    rows = await conn.fetch(
        f"""
        SELECT e.id, e.kind, e.domain, e.generation, e.container_id,
               e.terminal_command, e.task, e.workspace_source,
               e.workspace_path,
               {fairness.LAST_ADVANCED_SQL} AS {fairness.LAST_ADVANCED}
        FROM executions e
        WHERE e.state = 'RUNNING'
          AND e.container_id IS NOT NULL
          AND ($1::text IS NULL OR e.goal_id = $1)
        ORDER BY e.id
        """,
        goal_id,
    )
    return fairness.forget_the_key(
        fairness.least_recently_advanced([dict(r) for r in rows]))


async def _budget_mark(conn, execution_id: int) -> int:
    """Where the convergence budget starts counting, for this leg.

    TWO MARKS, AND THE LATER ONE WINS.

    `phase_generation` is where this PHASE began -- a review must not
    inherit the executor's leg count, or a story that took nine legs
    would get three review legs by arithmetic accident.

    `convergence_generation` is where the last PERSON answered a park.
    The ceiling is for a run that will not converge while nobody is
    watching; a human decision means somebody is. Without this, twelve
    plan revisions, or four rounds of the acceptance card's own "ask for
    more work" button, ended the run as FAILED for "not converging".

    READ FROM THE ROW IN THE DATABASE, not from the dict the caller
    assembled. `claimable()` selects nine columns and neither mark is
    among them, so `row.get("phase_generation")` is None on that path --
    one cheap query here is worth more than a plumbing rule every future
    caller has to remember.
    """
    row = await conn.fetchrow(
        "SELECT COALESCE(phase_generation, 0) AS phase_mark, "
        "       COALESCE(convergence_generation, 0) AS human_mark "
        "  FROM executions WHERE id = $1", execution_id)
    if row is None:
        return 0
    return max(int(row["phase_mark"]), int(row["human_mark"]))


async def _legs_taken(conn, execution_id: int, since: int = 0) -> int:
    """Distinct generations journalled — how many legs this run has had.

    `since` counts only generations at or after a mark, which is what a
    PHASE needs. The review phase runs in the same execution as the work
    that preceded it, so counting from zero would charge a reviewer for
    every leg the executor took: a story that took nine legs would get
    three review legs by arithmetic accident, and one that took twelve
    would get none and fail instantly — at the one point where failing
    is the outcome the design exists to rule out.
    """
    return await conn.fetchval(
        "SELECT count(DISTINCT generation) FROM step_journal "
        "WHERE execution_id = $1 AND generation >= $2",
        execution_id, since) or 0


async def _drain_ledger(conn) -> list[dict]:
    """Kernel decisions to the audit ledger. Never raises into the tick.

    Same contract as the usage drain: the record must never be able to break
    the thing it records. Logged rather than swallowed, because an audit
    trail that silently stops looks exactly like a system doing nothing.
    """
    try:
        from kernel.ledger.drain import drain_all

        return await drain_all(conn)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("ledger drain skipped this tick: %s", exc)
        return []


async def _drain_usage(conn) -> list[dict]:
    """Executor spend to `token_calls`. Never raises into the tick.

    Accounting is a report, not a dependency: a story must keep moving when
    the ledger cannot be written. The failure is logged rather than
    swallowed, because a cost page that silently stops filling looks exactly
    like a system that is not spending anything.

    Returns the tick's action LIST, like every other pass here — this
    accumulator is `list[dict]`, and a pass returning a count instead would
    raise `TypeError` on the `+=` and take the whole tick down with it.
    Reported only when it moved something, so a quiet tick stays quiet.
    """
    try:
        from kernel.usage.drain import drain_executor

        moved = await drain_executor(conn)
        return [{"action": "usage_drain", "rows": moved}] if moved else []
    except Exception as exc:                                # noqa: BLE001
        logger.warning("usage drain skipped this tick: %s", exc)
        return []


async def run_one_leg(conn, driver, row: dict, run_leg_fn,
                      make_executor, adapter) -> dict:
    """Take this execution's lock and run one leg. For callers with no lock.

    ONE WORKER PER EXECUTION. `claimable` returns every RUNNING execution
    and E0 has two callers — the console's `_drive()` and the
    `_worker_forever` sweep — so without the lock both take the same leg.
    Seen live: two models calling tools against one workspace until the
    journal's idempotency key crashed the second.

    THE SWEEP NO LONGER COMES THROUGH HERE. C5 moved its lock up to
    `worker/advance.py`, because reachability and env setup had to be
    inside it too — the non-holding replica was probing a busy bridge and
    destroying the pod underneath the replica that held the leg. This
    remains the entry point for a caller that holds no lock of its own
    (`runner.drive`, the console) and is exactly that wrapper: take the
    lock, run `leg_body`, release. Re-taking it from `advance` would not
    be an error — advisory locks are re-entrant per session — but it
    would make "did anyone else have this?" unanswerable there, which is
    the question C5 is about.
    """
    async with leg_lock(conn, row["id"]) as held:
        if not held:
            # Not an error and not worth waiting for: somebody else has
            # this leg. Come back on the next tick.
            return {"execution_id": row["id"], "action": "busy",
                    "detail": "another worker is running this execution's leg"}
        return await leg_body(conn, driver, row, run_leg_fn,
                              make_executor, adapter)


async def leg_body(conn, driver, row: dict, run_leg_fn,
                   make_executor, adapter) -> dict:
    """One leg, for a caller that ALREADY HOLDS this execution's leg lock.

    Split out of `run_one_leg` so the lock can be taken earlier without
    being taken twice — see `worker/advance.py`. Calling this without the
    lock is the C5 bug with extra steps.
    """
    # THE LEG'S CONNECTION, on the row, for the same reason the bridge
    # port is: `make_executor` is synchronous and the row is what it is
    # given. Its sub-agent context used to close over the sweep's single
    # connection, which is invisible while legs are serial and is
    # "another operation is in progress" the moment they are not. Set
    # HERE, at the one funnel every leg goes through, rather than in the
    # sweep — `runner.drive` and the console reach a leg without the
    # sweep and were never given one.
    row["conn"] = conn
    # WHO, resolved once per leg and put on the row — the same
    # place and for the same reason the bridge port goes there.
    # `make_executor` is synchronous and has no connection, and
    # every caller building one would otherwise resolve identity
    # its own way, which is how two callers end up attributing the
    # same work to different people.
    from kernel.harness.identity import resolve as resolve_identity

    try:
        row["identity"] = await resolve_identity(conn, row["id"])
    except Exception as e:                                  # noqa: BLE001
        # Never fatal. A commit recorded as `unattributed` is worse
        # than one attributed correctly and far better than a leg
        # that would not run.
        logger.warning("execution %s: identity unresolved (%s)",
                       row["id"], e)
        row["identity"] = None
    return await _take_leg(conn, driver, row, run_leg_fn,
                           make_executor, adapter)


def _model_of(adapter) -> str | None:
    """The model id, or None when there is no provider in the picture.

    THE NULL EXECUTOR HAS NO ADAPTER. `adapter.name` on None is an
    AttributeError, and it is raised from the success path — so every
    null-executor leg died on a line that exists only to record provider
    health. A provider fact must never be able to break a run that has
    no provider.
    """
    if adapter is None:
        return None
    return getattr(adapter, "_model", None) or getattr(adapter, "name", None)


async def _opening(conn, row: dict, phase: str, legs: int,
                   ceiling: int) -> str:
    """The first message of a leg: the story's task, or a review brief.

    A REVIEWER OPENS ON SOMETHING ELSE ENTIRELY. `row["task"]` is the
    work to do, and the work is finished — handing it to a reviewer
    would ask it to do the story again. It gets the brief instead:
    what was asked, what was planned, what a person settled, what was
    claimed, and where the goal is, all read from the database.

    NOT A FILE IN THE WORKSPACE. The reviewer is about to `git status`
    and `git diff` that tree; a brief written into it becomes a change
    the review reports on.
    """
    if phase != "review":
        return row.get("task", "")
    from kernel.harness.review_brief import build

    try:
        return await build(conn, row["id"], row.get("story_id") or "",
                           row.get("goal_id") or "",
                           leg=legs + 1, of=max(ceiling, 1),
                           workspace=row.get("workspace_path") or "",
                           mounts=row.get("mounts") or ())
    except Exception:                                       # noqa: BLE001
        # NEVER THE THING THAT ENDS THE PHASE. A reviewer with a thin
        # brief can still read the diff and say what it found; a leg
        # that dies assembling one produces nothing at all.
        logger.exception("execution %s: review brief failed", row["id"])
        return ("Your review brief could not be assembled. Review from the "
                "workspace alone: run `story_diff` and read what changed. "
                "Say in your summary that you judged without the story's "
                "plan, decisions or claim.")


async def _take_leg(conn, driver, row: dict, run_leg_fn,
                    make_executor, adapter) -> dict:
    """The leg itself, under the lock. Callers use `run_one_leg`.

    `run_leg_fn`, `make_executor` and `adapter` are injected rather than
    imported so this is testable without a model or a sandbox — the loop's
    decisions are what matter here, not the leg's contents.
    """
    execution_id = row["id"]
    # THE PHASE IS THE THIRD KEY, and it is read per leg like everything
    # else below — the phase changes BETWEEN legs, which is exactly the
    # property `approved_calls` and `mounts` are re-read for.
    phase = (row.get("phase") or "")
    profile = resolve(row["kind"], row["domain"], phase)
    generation = row["generation"]

    # WHERE this execution's shim answers, AND whether its tree is on
    # this filesystem. Both live in `reachability.locate` now; the
    # version that was inline here asked `is_pod(driver)` -- "is the
    # tree in a pod" -- and read the answer as "so cross to it", which
    # is the opposite of the truth for a leg running inside that pod.
    # Execution 214 died on it. On the row rather than in
    # `build_executor` for the reason `approved_calls` gives above:
    # that function is synchronous and the row is what it is given,
    # while resolving an address is a call to the substrate.
    from kernel.worker.reachability import locate

    await locate(driver, row)

    # THE CEILING BELONGS TO THE PHASE, not to the execution. Counting
    # from `phase_generation` is what lets a review have three legs after
    # work that took nine (see `_legs_taken`).
    #
    # AND THE PROFILE GETS AN OPINION. This asked `max_legs()`, which is
    # the global default with no profile behind it, so `profile.max_legs`
    # — declared on `Profile`, plumbed through `loader`, and read by
    # `loop_terms.resolve_budget` — was never consulted at the one site
    # that ends a run. No profile sets it today, so preferring it here
    # changes nothing for existing work and is the only way a review's
    # own ceiling can be three.
    since = await _budget_mark(conn, execution_id)
    legs = await _legs_taken(conn, execution_id, since)
    # THE GOAL'S CHOICE, frozen at dispatch, for the WORK only. A review
    # keeps its own budget: a goal that asked for short work legs did not
    # ask for a review that cannot finish.
    from kernel.harness.models import leg_choice           # noqa: PLC0415

    goal_legs = {} if phase == "review" else await leg_choice.frozen(
        conn, execution_id)
    ceiling = goal_legs.get("max_legs") or profile.max_legs or max_legs()
    if ceiling != -1 and legs >= ceiling:
        # A PHASE THAT MUST DELIVER CANNOT FAIL. For the work itself,
        # FAILED is right: nobody is owed an answer by a run that would
        # not converge. A review is owed one — `done -> reviewed` is
        # gated on a review existing, so a review that ends FAILED
        # strands its story at `done` for ever, holding its thread, its
        # workspace and its node. It ends DONE, saying honestly that it
        # produced nothing.
        if phase == "review":
            from kernel.worker.review_finish import synthesise_not_produced

            # NEVER RAISES OUT OF THE CEILING. This is the path that
            # ENDS a runaway, so an exception here is the one that turns
            # a bounded phase into an unbounded one: the leg crashes,
            # the generation advances, the ceiling fires again, for
            # ever. Execution 245 did exactly that -- four legs and
            # climbing, every one dying on `permission denied for table
            # story_reviews` inside the thing meant to stop it.
            try:
                await synthesise_not_produced(
                    conn, execution_id,
                    f"the reviewer took {legs} legs without submitting")
            except Exception:                               # noqa: BLE001
                logger.exception(
                    "execution %s: could not record the exhausted review; "
                    "ending the phase anyway", execution_id)
                await conn.execute(
                    "UPDATE executions SET state = 'DONE', phase = NULL, "
                    "updated_at = now() WHERE id = $1 "
                    "AND state NOT IN ('CANCELLED', 'FAILED')",
                    execution_id)
            return {"execution_id": execution_id, "action": "review_exhausted",
                    "detail": f"{legs} review legs without a verdict; "
                              f"recorded not_produced"}
        await conn.execute(
            "UPDATE executions SET state = 'FAILED' WHERE id = $1", execution_id)
        return {"execution_id": execution_id, "action": "leg_ceiling",
                "detail": f"{legs} legs without converging"}

    # E6h: whether work is permitted yet. Read per leg — the approval
    # arrives BETWEEN legs, which is what parking is for.
    approved = None
    if profile.requires_plan:
        approved = await plan_is_approved(conn, execution_id)
        # AND WHAT THAT APPROVAL COVERS. The platform's canned
        # trunk-integration plan is approved by nobody -- it is the
        # same three steps on every trunk move, and asking a person to
        # re-read them teaches them to approve without reading. It is
        # safe because it is SCOPED, and the scope is read here, per
        # leg, exactly as the approval above is.
        from kernel.harness.park_approval import plan_scope

        row = dict(row, plan_scope=await plan_scope(conn, execution_id))
        # Its file, too: see `planfiles.mirror_canned_plan`.
        if row["plan_scope"] is not None:
            from kernel.harness.planfiles import mirror_canned_plan
            await mirror_canned_plan(conn, execution_id)

    # Ec3c — and WHICH CALLS a person has said yes to. Read here for the
    # same reason and it is the whole fix: chain stage 3 used to read this
    # from a field nothing ever wrote, so an approval could be demanded,
    # given, and never seen. The run refused the same call for twelve legs
    # and ended FAILED.
    from kernel.harness.permission import approved_calls as _approved_calls

    # Ec4a — and which HOSTS it may reach, from the story's own grants.
    # Loaded here for the same reason: a grant can arrive between legs.
    from kernel.workspace.egress import allowed_hosts as _allowed_hosts

    row = dict(row,
               approved_calls=await _approved_calls(conn, execution_id),
               allowed_hosts=await _allowed_hosts(conn, execution_id))

    # E7f — what this run may write, per mount. Loaded here rather than
    # in `claimable` because `make_executor` is synchronous and the row
    # is what it is given.
    from kernel.workspace.mounts import mounts_for
    row = dict(row, mounts=await mounts_for(conn, execution_id))

    # AND WHERE ITS TOOLS ARE. `setups.REGISTRY` declares `bins` so a
    # check can call `pytest` rather than `.venv/bin/pytest`; the
    # Verifier has always honoured that and the executor never did, so
    # the model wrote the form that worked in its own sandbox and the
    # verification pod -- where the environment lives on the volume, not
    # in the tree -- could not find it. Execution 219.
    #
    # Loaded here with the other per-leg facts, and `{}` on any failure:
    # this buys a PATH entry and must never be the thing that ends a leg.
    from kernel.worker.leg_env import for_execution as _sandbox_path

    row = dict(row, mounts_env=await _sandbox_path(
        conn, execution_id, row.get("workspace_path") or "",
        row.get("bridge_host") or ""))

    # WHICH MODEL THIS RUN RUNS ON. The worker is started with one
    # adapter and injects it everywhere; the story's or the goal's frozen
    # choice takes precedence, then a profile that names a model, then
    # the injected default. Read per leg for the same reason the
    # approvals above are -- and because until this call existed the
    # frozen choice was written by the UI, frozen by dispatch, and read
    # by nobody.
    from kernel.worker.model_choice import adapter_for_execution

    adapter = await adapter_for_execution(conn, execution_id, profile, adapter)
    # The goal's leg size, clamped to what THIS model can hold with room
    # for its reply. Passed as `token_budget`, which the leg resolver
    # already treats as "the person means it" and applies field by field.
    goal_tokens = leg_choice.tokens_for(goal_legs,
                                        adapter.capabilities.max_context)

    executor = make_executor(profile, row, approved)
    try:
        result = await run_leg_fn(
            conn, adapter=adapter, execution_id=execution_id,
            generation=generation, actor_id=f"worker-{execution_id}",
            messages=[{"role": "user",
                       "content": await _opening(conn, row, phase,
                                                 legs, ceiling)}],
            profile=profile, tool_executor=executor,
            **({"token_budget": goal_tokens} if goal_tokens else {}),
            # NO MEMORY OF THE WORK — BUT MEMORY OF ITS OWN READING.
            #
            # Rehydration injects the PREVIOUS leg's brief
            # (`loop_rehydrate` reads `generation - 1`). For the FIRST
            # review leg that previous leg is the executor's, and
            # carrying it in would hand the reviewer the author's own
            # account of what it did and why — a reviewer that has read
            # the defence before reading the code is not reviewing it.
            #
            # For the SECOND and THIRD it is the reviewer's own earlier
            # leg, and dropping that is a different mistake: measured on
            # execution 246, three legs took 29, 16 and 8 steps re-doing
            # the same exploration, each starting from nothing, because
            # this said `phase != "review"` and meant no memory at all.
            # Independence is from the EXECUTOR, not from itself.
            rehydrate=(generation > 0
                       and (phase != "review" or generation > since)),
        )
    except ProviderUnavailable as e:
        # AN OUTAGE IS NOT A FAILED ATTEMPT, so this must not do what the
        # handler below does.
        #
        # Advancing the generation is right for a crash — the attempt is
        # over and its partial journal is the record. It is wrong here:
        # nothing about this run failed, and with MAX_LEGS at 12 and a 4s
        # tick, advancing on every outage tick burns the entire ceiling
        # in about 48 seconds and lands a healthy story in FAILED with
        # nothing left to resume.
        #
        # So: keep the generation, keep the state, record WHY. The
        # breaker has already stopped calling the provider, so the next
        # ticks cost nothing, and the story resumes on its own when the
        # probe succeeds.
        logger.warning("execution %s: provider unavailable at generation "
                       "%s (%s); holding, generation NOT advanced",
                       execution_id, generation, e.kind)
        await conn.execute(
            "UPDATE executions SET stall_reason = $2, "
            "stalled_at = COALESCE(stalled_at, now()) WHERE id = $1",
            execution_id, e.kind)
        # And where a DIFFERENT process can see it. The in-memory breaker
        # stops THIS process calling; only a durable row stops the
        # Conductor admitting more work into the same outage.
        model = _model_of(adapter)
        if model:
            await _health.mark_open(conn, model, e.kind)
        return {"execution_id": execution_id, "action": "provider_unavailable",
                "detail": f"{e}; generation held at {generation} so no leg "
                          f"is spent on someone else's downtime"}
    except Exception as e:                                  # noqa: BLE001
        # A LEG THAT DIED IS STILL A LEG THAT ENDED.
        #
        # The generation advance used to be the last statement in this
        # function, so an exception skipped it — and the next attempt
        # re-ran the SAME generation, collided on step 0's idempotency
        # key, and raised again. Every mid-leg failure wedged the run
        # permanently: a provider 500, a dropped container, a lost
        # network, all the same. Observed as a console retrying one
        # execution every four seconds indefinitely.
        #
        # A generation is an attempt at a leg. A crashed attempt is over,
        # so the next one is a new generation — its partial journal stays
        # where it is, which is the record of what actually happened.
        # Crash-looping now converges on the leg ceiling and FAILED
        # instead of spinning.
        logger.exception("execution %s: leg raised at generation %s",
                         execution_id, generation)
        await conn.execute(
            "UPDATE executions SET generation = generation + 1 WHERE id = $1",
            execution_id)
        return {"execution_id": execution_id, "action": "error",
                "detail": f"leg raised at generation {generation} "
                          f"({type(e).__name__}: {e}); generation advanced "
                          f"so the next attempt is a fresh leg"}

    # THE LEG RETURNED, SO WHATEVER STALLED IT IS OVER.
    #
    # Cleared here rather than where the outage is detected, because the
    # only evidence that a provider is back is a call that worked. A
    # stall_reason left behind after recovery is worse than none: it
    # reports the last bad hour as the current state, and the console
    # shows a healthy run as stalled for ever.
    await conn.execute(
        "UPDATE executions SET stall_reason = NULL, stalled_at = NULL "
        "WHERE id = $1 AND stall_reason IS NOT NULL", execution_id)
    model = _model_of(adapter)
    if model:
        await _health.mark_closed(conn, model)

    # ── Parked: the run is waiting on a human. Leave it alone. ───────────
    if result.get("parked"):
        return {"execution_id": execution_id, "action": "parked",
                "detail": result["parked"]["summary"]}

    # ── Completion proposed: a story goes to VERIFYING and waits. ────────
    #
    # UNLESS THE PLATFORM HAS VERIFICATION SWITCHED OFF, which is the
    # default. Then this falls through to `_finish_unverified` below --
    # the path errands have always taken: measure the claim against the
    # plan's OWN checks, publish, and park for a person to accept or ask
    # for more.
    #
    # ONE DECISION POINT. The switch is consulted here and nowhere else
    # on the way in, so `VERIFYING` is unreachable rather than reachable
    # and then abandoned. Everything downstream -- the adjudicator's
    # `state = 'VERIFYING'` scan, the claim, the pod, the prepare Job --
    # finds nothing to do without needing to know a switch exists.
    #
    # THE PROFILE STILL WINS WHERE IT SAYS NO. An errand has
    # `has_verifier = false` in its own toml; the switch can only ever
    # turn verification OFF, never on for a profile that does not want
    # it.
    from kernel.conductor.verification_switch import (          # noqa: PLC0415
        enabled as verification_enabled)

    if (result.get("proposals") and profile.has_verifier
            and await verification_enabled(conn)):
        state = await conn.fetchval(
            "SELECT state FROM executions WHERE id = $1", execution_id)
        if state == "RUNNING":
            await conn.execute(
                "UPDATE executions SET state = 'VERIFYING' WHERE id = $1",
                execution_id)
        return {"execution_id": execution_id, "action": "verifying",
                "detail": "waiting for a Verifier (Stage F)"}

    # ── Ec2e: no Verifier — check the claim here, then ask a person. ─────
    #
    # This branch did not exist, and its absence is bug 16: `proposals`
    # was tested together with `has_verifier`, so an errand's
    # `propose_completion` was IGNORED. It then fell through to the
    # terminal-command branch, `start_story` never sets one, and the run
    # exhausted its legs and ended FAILED with "12 legs without
    # converging" — a successful errand reported as a model failure.
    if result.get("proposals") and profile.allows_completion:
        return await _finish_unverified(conn, driver, row, execution_id,
                                        generation)

    # ── An errand's terminal command is the verdict (R3). ────────────────
    command = row.get("terminal_command")
    if command and not profile.has_verifier:
        exit_code, output = await _run_terminal(driver, row, command)
        if exit_code == 0:
            await apply_verdict(conn, execution_id, profile,
                                verdict_from_exit(0, output), command)
            return {"execution_id": execution_id, "action": "done",
                    "detail": f"terminal command exited 0"}
        # The same ceiling the run's own legs answer to, not the bare
        # global one: the profile's, then the goal's.
        ceiling = goal_legs.get("max_legs") or profile.max_legs or max_legs()
        if ceiling != -1 and legs + 1 >= ceiling:
            await apply_verdict(conn, execution_id, profile,
                                verdict_from_exit(exit_code, output), command)
            return {"execution_id": execution_id, "action": "failed",
                    "detail": f"terminal command exited {exit_code}"}
        # Non-zero and legs remain: give the agent another leg to fix it.

    # ── Otherwise: advance the generation and take another leg. ──────────
    await conn.execute(
        "UPDATE executions SET generation = generation + 1 WHERE id = $1",
        execution_id)
    return {"execution_id": execution_id, "action": "next_leg",
            "detail": f"generation {generation} -> {generation + 1}, "
                      f"ended on {result.get('predicate')}"}


async def _finish_unverified(conn, driver, row: dict, execution_id: int,
                             generation: int) -> dict:
    """Self-check the claim, then park on an acceptance. Ec2e.

    THE PERSON IS THE LAST WORD, and that is the whole shape the design
    asks for: the errand proposes completion, a person verifies and asks
    for more or accepts. So this never marks anything DONE — it parks, and
    `resolve_request` finishes the run when somebody approves.

    A claim that does not meet its own plan costs a LEG rather than
    somebody's attention. Nothing stops a model proposing completion on
    its first step; the checks it wrote are what make the claim mean
    something.
    """
    from kernel.harness.park import open_request
    from kernel.worker import selfcheck

    # THE RECORD, NOT THE TREE. Artifact criteria are answered from the
    # manifest; command criteria are not re-run here. Running them would
    # repeat, in the executor's own sandbox, a suite the executor has
    # just finished running as its work -- and it reports the numbers.
    # Turning platform verification on is what re-measures them, in a
    # clean container, which is the whole job of that switch.
    outcome = await selfcheck.evaluate(conn, execution_id)
    if not outcome["ok"]:
        from kernel.verifier.adjudicator import _tell_executor
        await _tell_executor(conn, execution_id, generation,
                             selfcheck.corrective(outcome), "selfcheck")
        await conn.execute(
            "UPDATE executions SET generation = generation + 1 WHERE id = $1",
            execution_id)
        logger.info("execution %s: self-check refused the claim: %s",
                    execution_id, outcome["summary"])
        return {"execution_id": execution_id, "action": "self_check_failed",
                "detail": outcome["summary"]}

    # ── DONE MEANS DELIVERED, ON THIS PATH TOO ───────────────────────
    #
    # `publish_execution` had exactly ONE caller -- `finish_pass.py`, on
    # the verified path. So every run that finished HERE was accepted
    # with its commits still local to the sandbox, and this is the
    # failure `finish_passed_claim` records in its own words: "The first
    # live story passed verification with its seven commits still local
    # to the sandbox -- the engine then merged the remote branch it had
    # cut, which held NOTHING, and announced a landing that never
    # happened."
    #
    # It did not bite while this path served only errands, most of which
    # write no repository. It becomes the default the moment
    # verification is switched off, and then it loses work.
    #
    # BEFORE THE GRANT CHECK, deliberately: a standing grant finishes the
    # run outright, so an unpublished grant-covered run would be DONE
    # with nothing pushed and nobody left to notice.
    from kernel.workspace.deliver import publish_execution

    delivered, detail = await publish_execution(conn, execution_id)
    if not delivered:
        from kernel.verifier.adjudicator import _tell_executor
        from kernel.worker import publish_block

        await conn.execute(
            "UPDATE executions SET stall_reason = $2 WHERE id = $1",
            execution_id, f"publish failed: {detail}"[:500])

        # WHOSE PROBLEM IS IT. A branch the executor can rebase is
        # handed back to the executor, exactly as before. A forge
        # refusal is not -- see publish_block, and the run parks on
        # the second one rather than spending legs looking for a cause
        # that is not in the repository.
        if not publish_block.is_executors(detail):
            count = await publish_block.note_forge_refusal(
                conn, execution_id)
            if count >= publish_block.PARK_AFTER:
                asked = await publish_block.park_on_forge_refusal(
                    conn, execution_id, generation, detail, count)
                if asked is not None:
                    return {"execution_id": execution_id,
                            "action": "publish_blocked",
                            "detail": f"the forge refused the push {count} "
                                      f"times; parked for a person: {detail}"}

        # THE LEG IS SPENT HERE AND NOT ABOVE. A parked run must not be
        # charged for the generation it never ran -- the budget exists
        # to bound the executor's attempts, and a park is the opposite
        # of an attempt.
        await conn.execute(
            "UPDATE executions SET generation = generation + 1 WHERE id = $1",
            execution_id)
        await _tell_executor(
            conn, execution_id, generation,
            "Your claim met its own checks but the work could not be "
            "pushed: " + detail + ". Check the branch state (diverged? "
            "protected?), resolve, and propose completion again.", "upub")
        logger.warning("execution %s: self-check passed but publish "
                       "FAILED: %s", execution_id, detail)
        return {"execution_id": execution_id, "action": "publish_failed",
                "detail": f"the claim held but the work could not be "
                          f"pushed: {detail}"}

    await _clear_publish_streak(conn, execution_id)

    # ── I5d WITHDRAWN: a completion is ALWAYS a person's ─────────────
    #
    # A standing grant used to finish the run here. The reasoning was
    # sound and is worth keeping in view: the self-check had measured
    # the claim against the plan's own checks, the grant was pinned to
    # that exact plan hash, and "the person is not skipped, they are
    # EARLIER -- they approved this plan, with these checks, and said it
    # may finish on its own."
    #
    # WHAT THAT MISSED is that finishing is not only a question of
    # whether the plan was carried out. It is also "is this enough?",
    # and nobody can answer that in advance, because the answer depends
    # on what the work FOUND. An errand that did exactly what its
    # approved plan said can still leave the obvious next question
    # unasked, and under the grant it went DONE with nobody reading it.
    #
    # So autonomy now means what it means everywhere else in this
    # system: A PERSON DOES NOT HAVE TO GO AND FIND THE CARD. It does
    # not mean nobody is asked. The run parks on an acceptance exactly
    # as a hand-started errand does, the GO is woken on that park, and
    # the GO can carry the answer with `decide_plan` once it has asked
    # a goal admin -- which is the whole point of that verb.
    #
    # The card already asks the right question and has done since it was
    # written: "Mark story done" or "Ask for more work". It simply never
    # reached an autonomous errand.
    #
    # THE PLAN GATE IS UNTOUCHED. `plan_is_approved` still honours the
    # grant, so run two onwards does not re-approve a plan a person has
    # already read. It is the COMPLETION that always asks.
    #
    # The proposal card's promise -- "finishing without being accepted
    # each time" -- was rewritten with this, because a card that
    # promises something the system no longer does is worse than no card.

    # The claim's identity, for the same reason a story's acceptance
    # carries its claim seq: `ask_question` dedupes on the hash, and
    # acceptance text is a constant. Without the generation in it, a
    # second proposal after a decline is refused as a repeat of the
    # first — leaving the run parked with nobody asked.
    # MERGE FIRST, ACCEPT AFTER -- for a coding story with work still to
    # land. Asking "is this finished?" before the work is on trunk asks
    # about something that is not yet anywhere; the merge is the
    # question that actually comes first. An errand owes no merge and is
    # asked exactly what it always was. See `completion_park`.
    from kernel.worker.completion_park import kind_for, payload_for

    park_kind = await kind_for(conn, execution_id)
    got = await open_request(
        conn, execution_id, generation, park_kind,
        # KIND-AWARE, because this path is no longer errands only. With
        # verification switched off a STORY finishes here too, and
        # "the errand says it is finished" is the wrong sentence to put
        # in front of somebody deciding about a story.
        payload_for(park_kind, work_kind=row.get("kind") or "story",
                    generation=generation, summary=outcome["summary"],
                    results=outcome["results"]),
        content_hash=f"{park_kind}-{execution_id}-{generation}")
    if isinstance(got, int):
        logger.info("execution %s: self-check passed -> awaiting %s "
                    "(question %s)", execution_id, park_kind, got)
        return {"execution_id": execution_id,
                "action": ("awaiting_merge" if park_kind == "landing"
                           else "awaiting_acceptance"),
                "detail": outcome["summary"]}

    # A GATE THAT OPENS WHEN ITS OWN MACHINERY FAILS IS NOT A GATE. The
    # story path learned this the hard way: falling through to DONE meant
    # a dedupe collision could finish a run nobody had accepted. Stall
    # loudly instead — nothing is lost by waiting.
    logger.error("execution %s: acceptance park refused (%s) — NOT marking "
                 "done; a person must still accept", execution_id, got)
    await conn.execute(
        "UPDATE executions SET stall_reason = $2, updated_at = now() "
        "WHERE id = $1",
        execution_id,
        f"finished its own checks, but the acceptance question could not be "
        f"opened: {getattr(got, 'message', got)}"[:500])
    return {"execution_id": execution_id, "action": "acceptance_blocked",
            "detail": "nobody could be asked to accept it — the run stays "
                      "unfinished rather than finishing itself"}


async def _run_terminal(driver, row: dict, command: str) -> tuple[int, str]:
    """Run the errand's declared command in its own sandbox."""
    try:
        result = await driver.exec(row["container_id"], "bash",
                                   {"command": command})
    except Exception as e:                                  # noqa: BLE001
        logger.warning("terminal command could not run: %s", e)
        return 1, f"the terminal command could not be run: {e}"
    return int(result.get("exit_code", 1)), result.get("output", "") or ""


async def worker_tick(conn, driver, run_leg_fn, make_executor, adapter,
                      goal_id: str | None = None,
                      max_concurrent: int = 1,
                      workspace_root: str = "",
                      acquire=None, supervisor=None) -> list[dict]:
    """One pass: take a leg for each claimable execution.

    CONCURRENT WHEN THE CALLER SUPPLIES `acquire`, serial otherwise. A leg is a
    model turn and its tool calls — seconds to minutes, nearly all of it
    waiting on a provider — so running them in turn made N stories cost N times
    the wall clock for work that was never CPU-bound.

    This used to say: "Serial by default. Concurrency is a real capability now
    that H1c gives each sandbox its own bridge port, but the ceiling belongs to
    admission (C7), not to this loop inventing a second one." The second
    sentence is right and was never an argument for the first. Admission
    decides how many runs may be IN FLIGHT; this decides how many of their legs
    execute AT ONCE. Honouring C7's ceiling exactly and still running the
    admitted legs together are not in tension — and the proof that they are
    not is four lines down, where `deliver_questions` was already given "a
    connection of their own" precisely because "this pass is serial, so
    answering inline would put every other story behind one person's question."
    A leg is also a model call taking minutes.

    `acquire` is a zero-argument callable returning an async context manager
    over a connection — `pool.acquire`. Each leg gets its own because
    `leg_lock` is connection-scoped and asyncpg forbids concurrent use of one
    connection. Callers with no pool keep the behaviour they had.

    `supervisor` decides whether this pass WAITS for the legs it starts. With
    one, legs are dispatched as background tasks and the sweep returns, so the
    next tick picks up whatever has become claimable since — which is the
    difference between "legs claimed together run together" and "runs are
    genuinely independent". Without one, the legs are gathered and awaited
    here, which is what every test wants.

    THREE MODES, most specific first: supervisor (dispatch), acquire alone
    (gather), neither (serial). They are not alternatives to choose between —
    each is the previous one plus a capability the caller supplied.
    """
    # H1a: answers first. An answer un-parks its run, and a run un-parked
    # at the top of this pass takes its next leg in the SAME pass rather
    # than waiting for the next one — which for a person watching the
    # console is the difference between "it resumed" and "did that work?".
    actions: list[dict] = list(await deliver_answers(conn, goal_id))
    # And the intents for runs with no leg to drain their own mailbox: a
    # story parked on a question, or waiting on a Verifier that does not
    # exist yet, can be abandoned by a person. Before machine v3 it could
    # not, and it held its admission slot for ever.
    actions += await deliver_cancellations(conn, goal_id)
    # And the pauses that raced a run out of RUNNING. A hold is taken by a
    # LEG; one submitted to a run that has already parked or gone to verify
    # has nobody to take it, and left pending it fires on some later leg —
    # the run pausing itself long after the person asked. Retired here so
    # the intent dies with the moment it was about.
    actions += await expire_stale_holds(conn, goal_id)
    # And the questions a person asked. Claimed here and answered on a
    # connection of their own: an answer is a model call taking minutes,
    # and this pass is serial, so answering inline would put every other
    # story behind one person's question.
    # `acquire`, so that "a connection of their own" comes from the POOL
    # and is counted against it. It was `asyncpg.connect` -- an
    # uncounted connection per question, on top of POOL_MAX, and the
    # failure mode of running out is not a clean refusal but every other
    # query in the process waiting, which reads as "the database got
    # slow".
    actions += await deliver_questions(conn, goal_id, acquire=acquire)
    # ── E12 · THE VERDICT IS NOT THIS PROCESS'S ANY MORE ──────────────
    #
    # `adjudicate_pass` ran here, and `dryrun_pass` under it. Both are
    # gone: the first moved to the verifier Deployment
    # (kernel/verifier/service.py) and the second retired with the dry
    # run itself (E9, plan 5.6).
    #
    # E1 deliberately left adjudication running in BOTH processes while
    # the new Deployment was scaffolding, and that was safe rather than
    # merely tolerated: `verdict_lock` is a per-EXECUTION advisory lock,
    # so cross-deployment exclusion was already true. What it cost was a
    # criterion container spent twice on the losing side of the race,
    # and a diagnosis that depended on the verifier's output landing in
    # the CONDUCTOR's log. Both end here.
    #
    # AND `finish_passed_claim` WENT WITH IT, which this comment used to
    # deny while naming the reason it could not. It is not in this tick
    # and never was: `verdict._apply` reaches it on the passing path and
    # the adjudicator's resume guard reaches it when a claim passed but
    # the run never left VERIFYING -- both INSIDE `adjudicate_pass`. So
    # moving adjudication moved publish and the acceptance park too;
    # there was nowhere for them to stay. Plan 5.4 step 9b claimed
    # otherwise and section 6 contradicted it; 5.18 records which was
    # right.
    #
    # Ec5: and the errands that are due to run again. On THIS tick rather
    # than a scheduler of their own — "only the Conductor dispatches" is an
    # invariant that has survived every stage, and it survives because
    # exactly one thing turns an intention into a container. The sweep
    # creates a PENDING execution and stops; admission, the kill switch and
    # every ceiling then apply as they would to a hand-started errand.
    from kernel.worker.schedules import sweep as _schedule_sweep

    # THE KILL SWITCH, and this is the first time it has reached the
    # kernel. `execution_pauses` is consulted by every dispatch path in
    # app/ and appeared nowhere under kernel/ -- so Freeze System turned
    # the banner red and stopped control-panel dispatch while admitted
    # executions kept taking legs and committing to git.
    #
    # Resolved HERE, after the delivery passes: answers and
    # cancellations must still land while frozen. Being unable to cancel
    # during a freeze would be perverse.
    #
    # "Finish what you are holding, take nothing new" -- so it gates the
    # schedule sweep, dispatch, and leg claiming, and nothing else. Legs
    # already in flight run to their boundary, where the workspace is
    # committed and the lease released.
    pause = await frozen(conn, goal_id)
    if pause is not None:
        actions.append({"execution_id": None, "action": "frozen",
                        "detail": describe(pause)})
        logger.warning("kill switch: %s", describe(pause),
                       extra={"event": "tick"})
        return actions

    actions += await _schedule_sweep(conn)
    # And the executor's spend into the cost ledger. A DRAIN and not a hook
    # in the harness: this must never be able to fail a leg, and draining
    # picks up everything already journaled rather than only what happens
    # next. Idempotent by unique key, so an overlapping tick or a re-run
    # cannot double a cost figure.
    #
    # Wired here for the reason the comment above gives — a pass without a
    # tick is an incomplete change, and this one would have shown up as a
    # cost page that was merely always a little out of date.
    actions += await _drain_usage(conn)
    # And the audit trail. Same shape and the same reasons: drained from the
    # tables the kernel already writes rather than emitted from call sites,
    # so it cannot fail a leg and cannot be missed by the next code path
    # somebody adds. `execution_transitions` is filled by a DATABASE trigger,
    # which makes it the one choke point every state change goes through.
    actions += await _drain_ledger(conn)
    # And admission. `admit_pending` had one caller — a person
    # pressing start — so an execution that became PENDING any other
    # way sat there for ever. An amended story goes DONE -> PENDING
    # deliberately, and with no periodic admission it stopped there:
    # "say what you want at any point" was true until the story had
    # finished, which is the case it was written for.
    # workspace_root is the CAPABILITY; goal_id is only a FILTER. The
    # old `goal_id and workspace_root` conflated them, so a worker
    # sweeping ALL goals (goal_id=None -- the standing local console)
    # never admitted anything: a run started by the assistant sat at
    # PENDING for ever while the sweep logged happily underneath.
    if workspace_root:
        from kernel.worker.runner import dispatch_pass
        # NO LIMIT PASSED, and that is the change. This handed dispatch
        # `leg_concurrency()` -- four -- so however many pods the cluster
        # could hold, four was the most that could be built per pass, and
        # each one blocked the pass for its whole startup. Dispatch
        # concurrency and LEG concurrency are different questions: one is
        # "how many pods may I build", the other "how many model turns may
        # run at once", and sharing a number meant neither could be tuned.
        # `dispatch_pass` now defaults to `dispatch_concurrency()` (24).
        actions += await dispatch_pass(conn, driver, goal_id,
                                       workspace_root)
        # S1 · AND SAY SO IF ANYTHING IS STILL WAITING. `reconcile`
        # declines PENDING and ADMITTED, so nothing else ages a row out:
        # an execution could wait an afternoon with `stall_reason` NULL
        # and the only evidence was somebody opening the board. Cheap --
        # one indexed read of rows older than ten minutes, and normally
        # none.
        from kernel.conductor.admit import report_stuck   # noqa: PLC0415

        actions += await report_stuck(conn, goal_id)
    rows = await claimable(conn, goal_id)

    # DISPATCH AND RETURN when the caller keeps a supervisor.
    #
    # Awaiting the legs here is what kept two stories serial even after they
    # each got their own connection: `_worker_forever` does not begin the next
    # sweep until this returns, so a run that becomes claimable while a batch
    # is in flight waits for the whole batch. Two seconds of skew between two
    # approvals was enough (KER17, runs 1550/1551) — see worker/supervisor.py.
    #
    # Legs already in flight are skipped rather than re-claimed. `leg_lock`
    # would refuse them anyway; this just avoids spending a connection to be
    # told so.
    if supervisor is not None and acquire is not None:
        busy = supervisor.in_flight()
        free = supervisor.free_slots(max_concurrent)
        for row in [r for r in rows if r["id"] not in busy][:free]:
            eid = row["id"]

            async def _leg(row=row):
                async with acquire() as leg_conn:
                    return await advance_one(leg_conn, driver, row,
                                             run_leg_fn, make_executor,
                                             adapter)

            if supervisor.spawn(eid, _leg()):
                actions.append({"execution_id": eid, "action": "leg_started",
                                "detail": f"{len(busy) + 1} leg(s) in flight"})
        return actions

    rows = rows[:max_concurrent]

    # SERIAL unless the caller handed us a way to get a connection per leg.
    #
    # `leg_lock` is an advisory lock held on a connection for the whole leg,
    # and asyncpg refuses concurrent use of one connection outright, so
    # parallel legs mean a connection each — there is no version of this that
    # shares `conn`. A caller with no pool (every test, the CLI) keeps exactly
    # the behaviour it had.
    if acquire is None or len(rows) < 2:
        for row in rows:
            actions += await advance_one(conn, driver, row, run_leg_fn,
                                         make_executor, adapter)
        return actions

    # Order is preserved, so `actions` reads the same as it did serially.
    from kernel.worker.concurrency import run_each_on_its_own_connection
    settled = await run_each_on_its_own_connection(
        acquire, rows,
        lambda leg_conn, row: advance_one(
            leg_conn, driver, row, run_leg_fn, make_executor, adapter))
    for row, got in zip(rows, settled):
        if isinstance(got, BaseException):
            logger.exception("worker: execution %s failed outside its own "
                             "handler", row["id"], exc_info=got)
            actions.append({"execution_id": row["id"], "action": "error",
                            "detail": f"{type(got).__name__}: {got}"})
        else:
            actions += got
    return actions
