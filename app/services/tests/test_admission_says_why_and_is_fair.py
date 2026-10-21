"""Nothing waits silently, and no goal takes the whole window.

Phase S. Four faults, all of them measured or read off the code, and all
of them shaped the same way: admission knew something and told nobody.

S1  A full bucket skipped with a bare `continue` -- no log, no row, no
    reason. `reconcile` explicitly declines PENDING and ADMITTED, so no
    sweep aged one out either. An execution could sit PENDING for an
    afternoon with `stall_reason` NULL, which is exactly how the
    zero-slots incident stayed invisible.

S2  Both queue reads were global `ORDER BY .. LIMIT 24`. Rows a full
    bucket would skip still consumed the limit, so ONE goal with 24+
    PENDING starved every other goal on every tick, for ever.

S3  Admission was purely polled, while NOTIFY was already used for
    `assistant_ask`, `story_done` and four others, with a real listener.

S4  Ceilings were per (goal, domain, kind) and NOTHING SUMMED THEM. Ten
    goals at four admits forty executions against a conductor that runs
    twenty legs -- twenty pods idle, holding quota, and no row saying so.
"""
from __future__ import annotations

import ast
import pathlib

from app.services.tests.shipped_tree import IS_DISTRIBUTION, baseline_mentions
from kernel.conductor import admit
from kernel.worker import service
from kernel.worker import service_wake as wake

ADMIT = pathlib.Path(admit.__file__).read_text(encoding="utf-8")
RUNNER = (pathlib.Path(admit.__file__).parents[1]
          / "worker" / "runner.py").read_text(encoding="utf-8")


# ── S4 · two ceilings ──────────────────────────────────────────────────

def test_there_is_a_cross_goal_ceiling_as_well_as_a_per_goal_one():
    assert admit.system_ceiling() > 0
    assert callable(admit._held_everywhere)


# The namespace ResourceQuota from `scripts/infra/gke_cluster.sh`. Copied
# rather than parsed out of the shell script: the point of these two tests
# is that somebody raising the ceiling is made to look at the quota, and a
# test that re-derives both sides of an inequality asserts nothing.
QUOTA_CPU = 128
QUOTA_PVCS = 120


def test_the_system_ceiling_is_argued_against_the_quota_not_a_leg_count():
    """D5. This used to read `<= MAX_LEG_CONCURRENCY * 2` -- a
    per-conductor connection-pool clamp doubled by a replica count, which
    described nothing once the leg moved into its own pod. Admission slots
    are the single ceiling now, so the thing they must fit inside is the
    namespace quota that actually refuses the pod."""
    from kernel.drivers.k8s_sqlproxy import CPUS
    from kernel.sizes import DEFAULT_SIZE, SIZES

    # D1 attached a SECOND cost curve to this ceiling (plan 11.5a): every
    # execution pod now carries a cloud-sql-proxy sidecar so the leg can
    # hold `leg_lock` on its own connection. It is a quarter of a core per
    # admitted execution and nothing else counts it.
    sqlproxy = int(CPUS.removesuffix("m")) / 1000
    assert sqlproxy == 0.25
    per_pod = SIZES[DEFAULT_SIZE].pod_cpus + sqlproxy
    assert admit.system_ceiling() * per_pod <= QUOTA_CPU


def test_the_retired_leg_ceiling_left_nothing_behind():
    """D5's actual worry, stated in the plan: "dead config is how a future
    ceiling gets set on a knob nothing reads". A constant that survives in
    one caller is exactly that, so the check is repo-wide.

    OVER IDENTIFIERS, NOT TEXT. Several files still name the retired
    constant in prose to record what went and why, which is the house
    rule and not a leftover. What must not exist is a binding, an import
    or a read of it -- that is the knob.
    """
    import ast as _ast
    import pathlib as _p

    live = []
    for root in (_p.Path("kernel"), _p.Path("app")):
        for f in root.rglob("*.py"):
            src = f.read_text(encoding="utf-8", errors="ignore")
            if "MAX_LEG_CONCURRENCY" not in src:
                continue
            for node in _ast.walk(_ast.parse(src)):
                named = (isinstance(node, _ast.Name)
                         and node.id == "MAX_LEG_CONCURRENCY")
                aliased = (isinstance(node, _ast.alias)
                           and node.name == "MAX_LEG_CONCURRENCY")
                if named or aliased:
                    live.append(f"{f}:{getattr(node, 'lineno', '?')}")
    assert not live, live


def test_every_admitted_execution_costs_a_pvc_too():
    """One volume per execution, and the quota counts them separately from
    CPU -- the verifier's prepare Jobs claim from the same 120."""
    assert admit.system_ceiling() <= QUOTA_PVCS


def test_the_two_ceilings_are_enforced_differently_on_purpose():
    """AND THE FIRST VERSION GOT THIS WRONG. It claimed a SECOND slot
    from a reserved `__system__` bucket, which the schema forbids:

        CREATE UNIQUE INDEX admission_slots_one_per_execution
            ON admission_slots (execution_id)
         WHERE execution_id IS NOT NULL AND released_at IS NULL

    One slot per execution is an invariant, and a good one -- it stops a
    run leaking concurrency by holding several. Claiming two raised
    `UniqueViolationError` out of `admit_pending` on EVERY tick, so
    admission stopped entirely for every execution. Measured live on 205,
    which sat PENDING while the log said "worker pass failed" four times
    a minute.

    Per-goal stays a ROW CLAIM: exact, race-free across replicas, and a
    product contract. Cross-goal is a COUNT, which may overshoot by one
    per replica -- acceptable, because it exists to stop the fleet
    admitting more than it can run, not to promise a number."""
    assert "_claim_slot" in ADMIT
    assert "FOR UPDATE SKIP LOCKED" in ADMIT
    assert "_held_everywhere" in ADMIT
    # CODE ONLY. The comment above `SYSTEM_CEILING` explains the design
    # that was rejected, so it NAMES `__system__` -- and an assertion
    # that something is absent will happily fire on the explanation of
    # why it is absent. Fourth time this trap was sprung today.
    code = chr(10).join(line for line in ADMIT.splitlines()
                        if not line.lstrip().startswith("#"))
    assert "__system__" not in code, (
        "a second slot per execution violates "
        "admission_slots_one_per_execution")


def test_the_cross_goal_ceiling_is_checked_first():
    """Coarser gate first, so a goal slot is never taken and then handed
    back because the system was full.

    READ INSIDE THE LOOP, not over the whole file: both gates are now
    helpers defined near the top, so a bare `.index` on either name
    finds its `def` and the ordering it measures is the order the
    functions happen to be written in, not the order they are called.
    """
    body = ADMIT[ADMIT.index("async def admit_pending"):]
    assert body.index("await _held_everywhere(conn)") < body.index(
        "await _claim_slot(")


def test_a_goal_bucket_miss_has_nothing_to_hand_back():
    """With the cross-goal ceiling as a count, a skipped row never held
    anything — so the leak the row-claim version had to guard against
    cannot happen at all."""
    body = ADMIT[ADMIT.rindex("if slot_id is None:"):]
    assert "system_slot" not in body[:900]


# ── S1 · nothing waits silently ────────────────────────────────────────

def test_both_ceilings_say_which_one_stopped_you():
    """'behind your own budget' and 'behind the whole system' need
    different answers from whoever is looking — the first they can
    shorten, the second they cannot.

    THE PER-GOAL ONE NAMES THE OWNER now. It used to say "at its ceiling
    for coding/story", the old (domain, kind) key; with every bucket
    written 'any'/'any' that sends the reader looking for a per-domain
    limit that no longer exists. What is full is one owner's budget.
    """
    # Fragments that do not span a line wrap -- the f-strings are split
    # across source lines, so the full sentences are not contiguous here.
    assert "the system is at its ceiling of" in ADMIT
    assert "{owner} already has " in ADMIT
    assert "which is its limit" in ADMIT
    assert "row['domain']}/{row['kind']}" not in ADMIT.split(
        "async def admit_pending")[1], (
        "the per-goal reason still names the retired (domain, kind) key")


def test_admission_clears_the_reason_when_it_admits():
    """A stall_reason left behind after the wait ended is worse than
    none: the row moves on and the board still explains why it could
    not."""
    block = ADMIT[ADMIT.index("SET state = 'ADMITTED'"):]
    assert "stall_reason = NULL" in block[:300]


def test_a_provider_outage_is_recorded_on_the_rows():
    assert "admission held:" in ADMIT


def test_a_dispatch_failure_reaches_the_row_not_just_a_log():
    """The comment at the handler records the symptom this leaves: 'sat
    at ADMITTED with no stall reason'."""
    assert "dispatch failed:" in RUNNER
    assert "stall_reason" in RUNNER


def test_something_finally_notices_a_long_wait():
    """`reconcile` declines PENDING and ADMITTED, so before this nothing
    anywhere aged a row out."""
    assert admit.stuck_after_s() >= 60
    assert callable(admit.report_stuck)
    assert "admission_stuck" in ADMIT


def test_a_row_with_no_reason_at_all_gets_one():
    """The worst case and the one the detector exists for: nothing
    refused it, so nothing explained it either."""
    assert "no admission pass has reached this execution" in ADMIT


def test_recording_a_reason_can_never_fail_admission():
    """Admission must not fail because the explanation could not be
    written."""
    fn = next(n for n in ast.walk(ast.parse(ADMIT))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_stalled")
    assert any(isinstance(h, ast.ExceptHandler) for h in ast.walk(fn))


# ── S2 · fairness ──────────────────────────────────────────────────────

def test_no_goal_may_fill_the_admission_window():
    assert "PARTITION BY goal_id" in ADMIT
    assert admit.goal_share() > 0


def test_the_window_function_is_not_where_the_lock_is_taken():
    """Postgres refuses FOR UPDATE alongside a window function, so the
    ranking sits in a subquery and the outer scan takes the lock -- which
    is also what SKIP LOCKED needs to stay correct across replicas."""
    ranked = ADMIT.index("ROW_NUMBER() OVER")
    locked = ADMIT.index("FOR UPDATE SKIP LOCKED", ranked)
    between = ADMIT[ranked:locked]
    assert ") ranked WHERE rn <=" in between


def test_the_window_is_the_size_of_the_largest_per_goal_ceiling():
    """Two mistakes, one number.

    TOO SMALL and the ceiling is unreachable: the GO may run ten
    autonomous errands in a goal, but if only eight of that goal's rows
    are scanned per pass it creeps toward ten a few at a time and looks
    mysteriously slow, with nothing anywhere saying why. This bound used
    to be `<= 8`, written when every ceiling was four.

    TOO LARGE and the window is spent on rows that could not be admitted
    regardless -- which is the starvation this share exists to stop.

    So: exactly the largest ceiling any one owner can hold.
    """
    from kernel.conductor.owner import ASSISTANT_CEILING, CEILINGS

    largest = max(list(CEILINGS.values()) + [ASSISTANT_CEILING])
    assert admit.goal_share() == largest


# ── S3 · evented, with the poll still underneath ───────────────────────

def test_the_tick_can_be_woken_by_an_insert():
    # Still reachable through `service`, which re-exports it -- the
    # verifier service imports several of these names from there and
    # the facade is what keeps that true (D2a).
    assert service.PENDING_CHANNEL == "execution_pending"
    assert callable(service.listen_for_pending)


def test_the_poll_survives_as_the_backstop():
    """A notification that never arrives -- listener reconnecting, a
    conductor restarting -- must cost latency and never an execution."""
    # READ THE MODULE THAT DEFINES IT, not the one that names it.
    # The sleep and the listener left `service.py` for `service_wake`
    # (D2a); a guard reading the file its subject has left does not
    # fail, it passes on no evidence.
    src = pathlib.Path(wake.__file__).read_text(encoding="utf-8")
    assert "timeout=interval" in src
    assert "admission falls back to" in src


def test_failing_to_listen_is_not_fatal():
    """A conductor that cannot listen is a conductor that polls, which is
    how it worked before."""
    fn = next(n for n in ast.walk(
        ast.parse(pathlib.Path(wake.__file__).read_text(encoding="utf-8")))
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "listen_for_pending")
    assert any(isinstance(h, ast.ExceptHandler) for h in ast.walk(fn))


def test_the_migration_is_sequenced():
    root = pathlib.Path(admit.__file__).parents[2]
    if IS_DISTRIBUTION:
        # pg_dump writes the trigger on one line and the function apart.
        assert baseline_mentions(
            r"CREATE TRIGGER execution_pending_notify AFTER INSERT ON "
            r"kernel\.executions FOR EACH ROW EXECUTE FUNCTION "
            r"kernel\.notify_execution_pending\(\)")
        assert baseline_mentions(r"pg_notify\('execution_pending'")
        return
    seq = (root / "migrations" / "SEQUENCE").read_text(encoding="utf-8")
    assert "execution_pending_notify" in seq
    sql = (root / "migrations"
           / "execution_pending_notify.sql").read_text(encoding="utf-8")
    # AFTER INSERT: a row is born PENDING, and by the time anything
    # updates it, it has already been seen.
    assert "AFTER INSERT ON kernel.executions" in sql
    assert "pg_notify('execution_pending'" in sql
