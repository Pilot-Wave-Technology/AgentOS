"""Admission — PENDING → ADMITTED via slot claim.

Scans kernel.executions for PENDING rows and attempts to claim a free
admission slot for each. On success, transitions the execution to ADMITTED.

CRITICAL: admit_pending MUST be called inside an existing
``async with conn.transaction()`` block. The caller (conductor_tick)
owns the transaction boundary. WITHOUT a transaction, FOR UPDATE SKIP
LOCKED is a no-op under asyncpg's autocommit — row locks release
immediately and two conductor replicas double-claim the same row.
C2's null executor has a comment in capitals saying "do NOT wrap in
conn.transaction()" — that advice is for the executor, not the Conductor.
"""

from __future__ import annotations

import logging
import os

from kernel.conductor.admission_limit import admit_batch
from kernel.harness.models.health import open_models

from kernel.conductor.owner import resolve as resolve_owner

logger = logging.getLogger(__name__)


# ── S4 · the second ceiling ──────────────────────────────────────────
#
# Ceilings were per (goal, domain, kind) and NOTHING SUMMED THEM. Ten
# goals at four admits forty executions -- forty pods, forty PVCs, ~60
# CPU -- against a conductor that can run twenty legs. Twenty pods then
# sit idle holding quota and money, and no row anywhere says why.
#
# A COUNT, NOT A SECOND SLOT -- and the first version got this wrong.
#
# The obvious design was a reserved `__system__` bucket claimed with the
# same atomic UPDATE as the per-goal one. It is wrong, and the schema
# says so:
#
#     CREATE UNIQUE INDEX admission_slots_one_per_execution
#         ON admission_slots (execution_id)
#      WHERE execution_id IS NOT NULL AND released_at IS NULL
#
# ONE SLOT PER EXECUTION is an invariant, and a good one -- it is what
# stops a run leaking concurrency by holding several. Claiming two threw
# `UniqueViolationError` on the second, out of `admit_pending`, on every
# tick, for every execution. Admission stopped entirely. Measured on 205.
#
# So the two ceilings are enforced two different ways, deliberately:
#
#   per-goal    a ROW CLAIM. Exact, race-free across replicas via
#               FOR UPDATE SKIP LOCKED. It is a product contract -- "this
#               goal may run four stories" -- and must never be exceeded.
#
#   cross-goal  a COUNT of held slots, checked before the claim. Two
#               replicas admitting in the same instant can each see
#               `held = 15` and both admit, so the global ceiling may
#               overshoot by at most one per replica. That is ACCEPTABLE
#               here and not there: this ceiling exists to stop the
#               fleet admitting more than it can run, and 17 instead of
#               16 does not break anything. Making it exact would need a
#               lock held across the whole pass, which would serialise
#               admission between the replicas to buy nothing.

# How many executions may be admitted across ALL goals at once.
#
# THIS IS THE CEILING. It used to be described as sitting "below the leg
# ceiling (MAX_LEG_CONCURRENCY x replicas = 20)", and D5 retired that
# number -- it was a per-conductor connection-pool clamp wearing a
# ceiling's name, and after D the leg runs in its own pod, where a
# conductor-side leg count governs nothing. So there is no second number
# to sit below: admission slots are the ONE documented control over how
# much the fleet runs at once, and anything that wants to change that
# amount changes this.
#
# WHAT SIXTEEN COSTS, because a ceiling with no price is a ceiling nobody
# argues with. Per admitted execution, at the default `small` tier
# (kernel/sizes.py): 1.0 vCPU sandbox + 0.5 worker + 0.25 sqlproxy =
# 1.75 vCPU, 4096 + 2048 + 512 = 6.5 GiB, and one PVC. Sixteen of those
# is 28 vCPU, 104 GiB and 16 PVCs against the namespace ResourceQuota in
# `scripts/infra/gke_cluster.sh` -- 128 CPU, 512 GiB, 120 PVCs, 150 Jobs
# -- which leaves room for the verifier's Jobs and the prepare Jobs that
# share it.
#
# THE SQLPROXY QUARTER-CORE IS D1'S, AND IT IS NEW (plan 11.5a). Every
# execution pod carries a cloud-sql-proxy sidecar at 250m/512Mi so the
# leg can hold `leg_lock` on its own connection. At this ceiling that is
# 4 vCPU and 8 GiB spent holding sixteen connections. It is the second
# cost curve attached to this number and it is stated here rather than
# only in the driver, because this is where somebody raises the number.
#
# AND THE TIER IS THE VARIABLE, NOT THIS. Sixteen `xlarge` pods are
# 16 x 8.25 = 132 vCPU and 520 GiB, which is OVER the quota on both --
# the quota, not this constant, is what stops the sixteenth one, and it
# stops it by refusing to schedule rather than by saying why. A soak that
# raises `AGENTOS_SYSTEM_CEILING` must be read against the tier mix it
# will actually admit.
SYSTEM_CEILING = 20


def system_ceiling() -> int:
    """Read per call, so a soak can raise it without a restart."""
    if "AGENTOS_SYSTEM_CEILING" not in os.environ:
        return SYSTEM_CEILING
    try:
        return max(1, int(os.environ["AGENTOS_SYSTEM_CEILING"]))
    except ValueError:
        return SYSTEM_CEILING


# ── S2 · how much of one pass any single goal may occupy ─────────────
#
# Both queue reads were global `ORDER BY .. LIMIT 24`. A goal with 24+
# PENDING rows and a full bucket filled the whole window EVERY TICK,
# FOREVER: its rows are skipped at the slot claim but still consume the
# limit, so no other goal's execution is ever seen, admitted, or even
# given a bucket.
#
# TEN, BECAUSE THAT IS THE LARGEST CEILING ONE OWNER CAN HOLD -- the
# GO's ten autonomous errands per goal. Scanning more of one goal is
# window spent on rows that could not be admitted regardless; scanning
# fewer makes the ceiling unreachable, and the run would creep toward
# ten a few per pass and merely look slow. It was four when every
# ceiling was four, and the two numbers have to move together.
GOAL_SHARE = 10


def goal_share() -> int:
    if "AGENTOS_GOAL_SHARE" not in os.environ:
        return GOAL_SHARE
    try:
        return max(1, int(os.environ["AGENTOS_GOAL_SHARE"]))
    except ValueError:
        return GOAL_SHARE


async def ensure_bucket(conn, goal_id: str, owner: str) -> None:
    """Admission needs a bucket AND ITS SLOTS, keyed by OWNER.

    RE-KEYED FROM (goal, domain, kind). That combination answered "how
    much coding work may this goal run", which nobody asked: a goal
    doing two kinds held two independent budgets, and a goal doing one
    was capped at four however many people waited. The limit people want
    is per WHO -- the GO's autonomous errands, or one person's
    assistant -- and per goal, so the same person in two goals gets a
    budget in each.

    A goal missing either the bucket or its slots is admitted by
    NOTHING, and the run sits at PENDING with no log line saying why.
    KER04's first live story did exactly that. Measured on `next` on
    2026-09-16: eight of thirteen buckets had zero slots, and execution
    194 sat PENDING with `stall_reason` NULL and no error anywhere.
    So this checks the SLOTS, not merely the bucket's existence -- an
    early return on the bucket alone makes a slotless goal unrepairable.

    THE ROW'S CEILING WINS over the default, so widening a goal by hand
    is not undone by the next tick.
    """
    from kernel.conductor.owner import ceiling_for

    default = ceiling_for(owner)
    await conn.execute(
        "INSERT INTO admission_buckets (goal_id, owner, domain, kind, "
        "                               ceiling) "
        "VALUES ($1, $2, 'any', 'any', $3) ON CONFLICT DO NOTHING",
        goal_id, owner, default)

    effective = await conn.fetchval(
        "SELECT ceiling FROM admission_buckets "
        " WHERE goal_id = $1 AND owner = $2", goal_id, owner) or default

    have = await conn.fetchval(
        "SELECT count(*) FROM admission_slots "
        " WHERE goal_id = $1 AND owner = $2", goal_id, owner) or 0
    if have >= effective:
        return

    for i in range(effective):
        await conn.execute(
            "INSERT INTO admission_slots (goal_id, owner, domain, kind, "
            "                             ceiling, slot_index) "
            "VALUES ($1, $2, 'any', 'any', $3, $4) ON CONFLICT DO NOTHING",
            goal_id, owner, effective, i)
    logger.info("admission: %s/%s had %d of %d slots; wrote the rest",
                goal_id, owner, have, effective)


async def _claim_slot(conn, execution_id: int, goal: str, owner: str):
    """One free slot in one owner's bucket, atomically. None means full.

    `FOR UPDATE SKIP LOCKED` is what makes this correct with two
    conductors: the row IS the claim, so two replicas cannot both win it,
    and neither waits for the other.
    """
    return await conn.fetchval(
        """
        UPDATE admission_slots
        SET execution_id = $1, claimed_at = now(), released_at = NULL
        WHERE id = (
            SELECT id FROM admission_slots
            WHERE goal_id = $2 AND owner = $3
              AND (execution_id IS NULL OR released_at IS NOT NULL)
            ORDER BY slot_index
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id
        """,
        execution_id, goal, owner)


async def _held_everywhere(conn) -> int:
    """How many executions hold an admission slot right now, all goals.

    The cross-goal ceiling. One indexed count over a table whose live
    rows number in the tens -- the held set IS the concurrency, so it
    cannot grow with history the way `executions` does.
    """
    return await conn.fetchval(
        "SELECT count(*) FROM admission_slots "
        "WHERE execution_id IS NOT NULL AND released_at IS NULL") or 0


async def _release_slot(conn, slot_id) -> None:
    """Hand a slot back. For the claim that was taken and not used."""
    if slot_id is None:
        return
    await conn.execute(
        "UPDATE admission_slots SET released_at = now() WHERE id = $1",
        slot_id)


async def _stalled(conn, execution_id: int, why: str) -> None:
    """Say why this row is still PENDING. NEVER RAISES.

    S1. A full bucket skipped with a bare `continue`: no log, no row, no
    reason. An execution could sit PENDING indefinitely with
    `stall_reason` NULL, which is exactly how the zero-slots incident
    stayed invisible -- and `reconcile` explicitly declines PENDING, so
    no sweep was ever going to notice either.

    Best effort on purpose: admission must not fail because the
    explanation could not be written.
    """
    try:
        await conn.execute(
            "UPDATE executions SET stall_reason = $2, "
            "stalled_at = COALESCE(stalled_at, now()) "
            "WHERE id = $1 AND state = 'PENDING'",
            execution_id, why[:500])
    except Exception:                                       # noqa: BLE001
        logger.warning("could not record why %s is waiting", execution_id,
                       exc_info=True)


async def admit_pending(conn, goal_id: str | None) -> list[int]:
    """Find PENDING executions and claim admission slots for them.

    Must be called inside ``async with conn.transaction()``.
    Returns list of execution ids that were successfully admitted.

    BOUNDED PER PASS (C4). The bucket ceilings bound one (goal, domain,
    kind) and nothing bounded the pass, so a system-wide burst admitted
    about forty executions at once and dispatch provisioned about forty
    pods, thirty-six of them idle and each holding a PVC. See
    `admission_limit.admit_batch` for why the answer is a batch bound
    rather than a global in-flight count. Whatever is left over is still
    PENDING and still first in line: admission is level-triggered and
    runs every tick, so the bound is a rate, not a refusal.
    """
    batch = admit_batch()
    # ── Fb4b: do not admit into an outage ────────────────────────────────
    # A slot claimed now is a story that dispatches, takes a leg, gets
    # ProviderUnavailable and stalls — holding an admission slot for the
    # duration. That is worse than waiting: the ceiling on concurrent
    # work fills with runs that cannot run, and the goal is blocked
    # behind them when the provider returns.
    #
    # Read from the table rather than the in-memory breaker on purpose.
    # E0 happens to run the worker and the Conductor in one process, so
    # the object would work today and stop working the moment Stage H
    # splits them — with nothing going red to say so.
    #
    # Stale rows are ignored by `open_models`, which fails OPEN: a
    # worker killed mid-outage must not block admission for ever.
    down = await open_models(conn)
    if down:
        first = down[0]
        # Level-triggered and called every tick, so this must not shout
        # every four seconds for the length of an outage.
        logger.info("admission held for goal %s: %s unavailable (%s, %.0fs)",
                    goal_id, first["model"], first["kind"], first["age_s"])
        # S1 · AND ON THE ROWS. An outage held the whole fleet with one
        # INFO line and nothing per execution, so a PENDING row looked
        # identical to one nobody had got to yet.
        try:
            await conn.execute(
                """
                UPDATE executions SET stall_reason = $2,
                       stalled_at = COALESCE(stalled_at, now())
                 WHERE state = 'PENDING'
                   AND ($1::text IS NULL OR goal_id = $1)
                   AND stall_reason IS DISTINCT FROM $2
                """,
                goal_id,
                f"admission held: {first['model']} unavailable "
                f"({first['kind']})"[:500])
        except Exception:                                   # noqa: BLE001
            logger.warning("could not record the admission hold",
                           exc_info=True)
        return []

    # ── Find pending executions in FIFO order ────────────────────────────
    # WHERE goal_id = $1 filters to the requested goal. This also excludes
    # NULL-goal_id rows (test_null_executor.sh inserts executions without
    # goal_id). The eventual correct shape is a global scan — S7 keys the
    # bucket by goal, not the scan. Whoever removes the goal_id filter
    # must add AND goal_id IS NOT NULL or leftover rows poison every tick.
    rows = await conn.fetch(
        """
        SELECT id, kind, domain, goal_id, story_id
        FROM executions
        WHERE state = 'PENDING'
          AND ($1::text IS NULL OR goal_id = $1)
          -- The comment above was written for this exact change: with a
          -- global scan, NULL-goal fixture rows would poison every tick.
          AND goal_id IS NOT NULL
          -- S2 · NO GOAL MAY FILL THE WINDOW. Rows a full bucket will
          -- skip still consumed the LIMIT, so one goal with 24+ PENDING
          -- starved every other goal on every tick, for ever. Ranked per
          -- goal and cut at `$3` so each goal brings at most its own
          -- share and the rest of the window belongs to everybody else.
          --
          -- The window sits in a subquery because Postgres refuses FOR
          -- UPDATE alongside a window function; the lock is taken by the
          -- outer scan, which is what SKIP LOCKED needs.
          AND id IN (
              SELECT id FROM (
                  SELECT id,
                         ROW_NUMBER() OVER (PARTITION BY goal_id
                                            ORDER BY created_at, id) AS rn
                    FROM executions
                   WHERE state = 'PENDING'
                     AND ($1::text IS NULL OR goal_id = $1)
                     AND goal_id IS NOT NULL
              ) ranked WHERE rn <= $3
          )
        ORDER BY created_at, id
        -- created_at is tx-start time — identical within a batch. id breaks
        -- ties deterministically (S7). Do not remove ', id'.
        --
        -- C4: and only a batch of them. The bound is on the SELECT as
        -- well as on the loop below because FOR UPDATE SKIP LOCKED locks
        -- every row it RETURNS, for the length of the caller's
        -- transaction — an unbounded select locked the whole PENDING
        -- queue against the other replica on every tick. LIMIT after
        -- ORDER BY keeps it FIFO: the batch is the oldest N, never an
        -- arbitrary N.
        LIMIT $2
        FOR UPDATE SKIP LOCKED
        """,
        goal_id, batch, goal_share(),
    )

    admitted = []
    for row in rows:
        # THE CEILING ITSELF, distinct from the LIMIT above. The select
        # bounds what is LOCKED; this bounds what is ADMITTED, and it
        # stays correct if the query is ever loosened — which it is
        # scheduled to be, when the goal_id filter becomes the global
        # scan the comment above plans for.
        if len(admitted) >= batch:
            logger.info("admission pass full for goal %s at %d; the rest "
                        "stay PENDING for the next tick", goal_id, batch)
            break
        # Refuse NULL goal_id loudly — never skip.
        if row["goal_id"] is None:
            raise ValueError(
                f"execution {row['id']} has NULL goal_id — "
                f"admit_pending refuses NULL, never skips"
            )

        # ── S4 · THE SYSTEM CEILING FIRST ───────────────────────────
        # Cross-goal before per-goal, deliberately: the coarser gate
        # answers first, so a goal slot is never taken and handed back.
        # Counted rather than claimed -- see the note at SYSTEM_CEILING
        # for why a second slot row is not possible and not needed.
        if await _held_everywhere(conn) >= system_ceiling():
            # S1 · SAID OUT LOUD, and distinctly. "waiting behind your own
            # goal" and "waiting behind the whole system" need different
            # answers from whoever is looking, and before this they were
            # the same silence.
            await _stalled(conn, row["id"],
                           f"admission: the system is at its ceiling of "
                           f"{system_ceiling()} concurrent executions")
            continue

        # ── Claim one free slot in this OWNER's bucket ───────────────
        #
        # Keyed by who is running, not by what domain: the GO's
        # autonomous errands share one budget of ten per goal, each
        # person's assistant has five, and a person with assistants in
        # two goals gets five in each.
        #
        # The bucket is ensured HERE rather than at goal creation
        # because owners appear as work does -- a goal's second member
        # has no bucket until their first run, and a missing bucket
        # admits nothing while looking configured.
        owner = await resolve_owner(conn, row["id"], row["goal_id"],
                                    row["story_id"])
        await ensure_bucket(conn, row["goal_id"], owner)

        # Zero rows = refusal (bucket full). Same discipline as S4's
        # heartbeat rule: "matching no row means you have been fenced."
        slot_id = await _claim_slot(conn, row["id"], row["goal_id"], owner)

        if slot_id is None:
            # Bucket full — skip this execution, do not raise.
            #
            # Nothing to hand back: the system ceiling is a count, so a
            # skipped row never held anything.
            # NAME THE OWNER AND THE NUMBER. This used to say "at its
            # ceiling for coding/story", which was the old (domain, kind)
            # key -- and now that every bucket is written 'any'/'any' it
            # would send whoever read it looking for a per-domain limit
            # that does not exist. What is actually full is one owner's
            # budget in this goal: the GO's ten autonomous errands, or
            # one person's five. A person can act on that (finish one,
            # stop one); they cannot act on a domain.
            from kernel.conductor.owner import ceiling_for

            await _stalled(conn, row["id"],
                           f"admission: {owner} already has "
                           f"{ceiling_for(owner)} running in goal "
                           f"{row['goal_id']}, which is its limit; this "
                           f"one starts as soon as one of them finishes")
            continue

        # Transition PENDING → ADMITTED
        await conn.execute(
            """
            UPDATE executions
            -- AND CLEAR THE REASON. A stall_reason left behind after
            -- the wait ended is worse than none: the row moves on and
            -- the board still explains why it could not.
            SET state = 'ADMITTED', updated_at = now(),
                stall_reason = NULL, stalled_at = NULL
            WHERE id = $1 AND state = 'PENDING'
            """,
            row["id"],
        )
        admitted.append(row["id"])

    return admitted


# ── S1 · nothing watched how long a row had been waiting ─────────────
#
# `reconcile` explicitly declines PENDING and ADMITTED, so no sweep ever
# aged one out. An execution could sit PENDING for an afternoon with
# `stall_reason` NULL and the only evidence was somebody opening the
# board and noticing. The zero-slots incident lasted as long as it did
# for exactly this reason.
#
# A DETECTOR AND NOT A REAPER. Whatever is wrong -- a full bucket, a
# provider outage, an admission pass that is not running at all -- the
# answer is never "cancel the work". It is to say so loudly enough that
# somebody looks.
STUCK_AFTER_S = 600.0


def stuck_after_s() -> float:
    if "AGENTOS_PENDING_STUCK_S" not in os.environ:
        return STUCK_AFTER_S
    try:
        return max(60.0, float(os.environ["AGENTOS_PENDING_STUCK_S"]))
    except ValueError:
        return STUCK_AFTER_S


async def report_stuck(conn, goal_id: str | None = None) -> list[dict]:
    """Executions waiting far longer than they should. Says so, per row."""
    limit = stuck_after_s()
    rows = await conn.fetch(
        """
        SELECT id, goal_id, domain, kind, stall_reason,
               EXTRACT(EPOCH FROM (now() - created_at)) AS waited
          FROM executions
         WHERE state IN ('PENDING', 'ADMITTED')
           AND ($1::text IS NULL OR goal_id = $1)
           AND created_at < now() - ($2 || ' seconds')::interval
         ORDER BY created_at
         LIMIT 50
        """,
        goal_id, str(int(limit)))
    out = []
    for row in rows:
        # A row with no reason at all is the worst case and the one this
        # exists for: nothing skipped it on purpose, so nothing explained
        # it either.
        why = row["stall_reason"] or (
            "no admission pass has reached this execution -- nothing "
            "refused it, so nothing explained it either")
        logger.warning(
            "execution %s (%s %s/%s) has waited %.0fs to be admitted: %s",
            row["id"], row["goal_id"], row["domain"], row["kind"],
            row["waited"], why, extra={"event": "tick"})
        if not row["stall_reason"]:
            await _stalled(conn, row["id"], why)
        out.append({"execution_id": row["id"], "action": "admission_stuck",
                    "detail": f"waited {row['waited']:.0f}s: {why}"})
    return out


# ── C · slots held by executions that never transitioned ─────────────
#
# The release is a TRIGGER on `AFTER UPDATE OF state` into a terminal
# state. That covers every run that ends -- and nothing else. A worker
# killed between claiming a slot and writing the first state leaves a
# row that never updates again, so the trigger never fires and the slot
# is held for ever. With a ceiling of four, four such rows close a goal
# permanently, and the only symptom is admission that silently stops.
#
# Reaping by "the execution is terminal or gone" and NOT by age: a slot
# held by a live run is doing its job however long it takes, and a
# timeout here would be a second, quieter concurrency limit that nobody
# configured.
async def release_dead_slots(conn) -> list:
    """Free slots whose execution is finished or absent. Returns actions."""
    rows = await conn.fetch(
        """
        UPDATE admission_slots s
           SET released_at = now()
         WHERE s.execution_id IS NOT NULL
           AND s.released_at IS NULL
           AND NOT EXISTS (
                 SELECT 1
                   FROM executions e
                   JOIN run_state_machine m ON m.version = e.machine_version
                  WHERE e.id = s.execution_id
                    AND NOT (m.terminal_states @> to_jsonb(e.state))
               )
        RETURNING s.id, s.execution_id, s.goal_id, s.domain, s.kind
        """)
    out = []
    for row in rows:
        logger.warning(
            "admission slot %s freed: execution %s (%s %s/%s) is terminal "
            "or gone and the release trigger never fired for it",
            row["id"], row["execution_id"], row["goal_id"], row["domain"],
            row["kind"], extra={"event": "reconcile"})
        out.append({"execution_id": row["execution_id"],
                    "action": "slot_reclaimed",
                    "detail": f"{row['goal_id']} {row['domain']}/"
                              f"{row['kind']}"})
    return out
